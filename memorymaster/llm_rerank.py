"""LLM cross-encoder reranking for retrieval candidates."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Any

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from memorymaster.llm_provider import call_llm, parse_json_response

logger = logging.getLogger(__name__)

_MAX_SNIPPET_CHARS = 300
_LAST_CALL_AT = 0.0
_RATE_LOCK = threading.Lock()
_CONSECUTIVE_FAILURES = 0
_DISABLED = False
_STATS: dict[str, int] = {"attempts": 0, "successes": 0, "failures": 0, "disabled_fallbacks": 0}
_PROMPT = """Score how relevant each candidate is to the question.

Return STRICT JSON only: an array of [candidate_index, relevance_score] pairs.
candidate_index must match the numbered candidate. relevance_score must be an integer from 0 to 100.
Do not include prose or markdown."""


class LLMRerankError(RuntimeError):
    """Raised for retryable rerank judge failures."""


@contextmanager
def _temporary_llm_env(judge_model: str):
    saved_provider = os.environ.get("MEMORYMASTER_LLM_PROVIDER")
    saved_model = os.environ.get("MEMORYMASTER_LLM_MODEL")
    saved_key_file = os.environ.get("MEMORYMASTER_KEY_FILE")
    saved_key_rotation = os.environ.get("MEMORYMASTER_LLM_KEY_ROTATION")
    try:
        from memorymaster.key_rotator import clear_cache

        clear_cache()
        os.environ["MEMORYMASTER_LLM_PROVIDER"] = "google"
        os.environ["MEMORYMASTER_LLM_MODEL"] = judge_model
        os.environ["MEMORYMASTER_KEY_FILE"] = "__memorymaster_llm_rerank_no_key_file__"
        os.environ["MEMORYMASTER_LLM_KEY_ROTATION"] = "0"
        yield
    finally:
        if saved_provider is None:
            os.environ.pop("MEMORYMASTER_LLM_PROVIDER", None)
        else:
            os.environ["MEMORYMASTER_LLM_PROVIDER"] = saved_provider
        if saved_model is None:
            os.environ.pop("MEMORYMASTER_LLM_MODEL", None)
        else:
            os.environ["MEMORYMASTER_LLM_MODEL"] = saved_model
        if saved_key_file is None:
            os.environ.pop("MEMORYMASTER_KEY_FILE", None)
        else:
            os.environ["MEMORYMASTER_KEY_FILE"] = saved_key_file
        if saved_key_rotation is None:
            os.environ.pop("MEMORYMASTER_LLM_KEY_ROTATION", None)
        else:
            os.environ["MEMORYMASTER_LLM_KEY_ROTATION"] = saved_key_rotation
        clear_cache()


def _min_interval_seconds() -> float:
    raw = os.environ.get("MEMORYMASTER_LLM_RERANK_MIN_INTERVAL_SECONDS", "3.1").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 3.1


def _max_failures_before_disable() -> int:
    raw = os.environ.get("MEMORYMASTER_LLM_RERANK_MAX_FAILURES", "1").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def _pace_judge_call() -> None:
    global _LAST_CALL_AT
    min_interval = _min_interval_seconds()
    if min_interval <= 0:
        return
    with _RATE_LOCK:
        now = time.monotonic()
        sleep_for = max(0.0, min_interval - (now - _LAST_CALL_AT))
        if sleep_for > 0:
            time.sleep(sleep_for)
        _LAST_CALL_AT = time.monotonic()


def _candidate_text(candidate: Any) -> str:
    if isinstance(candidate, str):
        return candidate
    if isinstance(candidate, dict):
        claim = candidate.get("claim")
        if claim is not None:
            return str(getattr(claim, "text", "") or "")
        return str(candidate.get("text", "") or "")
    return str(getattr(candidate, "text", "") or candidate)


def _build_rerank_input(query: str, candidates: Sequence[Any]) -> str:
    lines = [f"Question: {query.strip()}", "", "Candidates:"]
    for idx, candidate in enumerate(candidates, start=1):
        snippet = " ".join(_candidate_text(candidate).split())[:_MAX_SNIPPET_CHARS]
        lines.append(f"{idx}. {snippet}")
    return "\n".join(lines)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=10, max=60),
    retry=retry_if_exception_type(LLMRerankError),
    reraise=True,
)
def _call_rerank_judge(query: str, candidates: Sequence[Any], judge_model: str) -> str:
    _pace_judge_call()
    with _temporary_llm_env(judge_model):
        response = call_llm(_PROMPT, _build_rerank_input(query, candidates))
    if not response.strip():
        raise LLMRerankError("empty rerank judge response")
    return response


def _score_entry(entry: Any) -> tuple[int, float] | None:
    if isinstance(entry, dict):
        raw_index = entry.get("candidate_index", entry.get("index"))
        raw_score = entry.get("relevance_score", entry.get("score"))
    elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
        raw_index, raw_score = entry[0], entry[1]
    else:
        return None
    try:
        index = int(raw_index)
        score = float(raw_score)
    except (TypeError, ValueError):
        return None
    return index, max(0.0, min(100.0, score))


def _parse_scores(response: str, candidate_count: int) -> dict[int, float]:
    scores: dict[int, float] = {}
    for entry in parse_json_response(response):
        parsed = _score_entry(entry)
        if parsed is None:
            continue
        index, score = parsed
        if 1 <= index <= candidate_count:
            scores[index - 1] = score
    return scores


def get_rerank_stats() -> dict[str, int]:
    return {**_STATS, "disabled": int(_DISABLED)}


def rerank_temporarily_disabled() -> bool:
    return _DISABLED


def rerank_with_llm(
    query: str,
    candidates: list[Any],
    top_k: int = 5,
    judge_model: str = "gemini-2.5-flash",
) -> list[Any]:
    """Rerank candidates with a batched Gemini relevance judge.

    Any judge failure returns the original ordering truncated to ``top_k``.
    """
    if top_k <= 0:
        return []
    if not query.strip() or not candidates:
        return candidates[:top_k]

    global _CONSECUTIVE_FAILURES, _DISABLED
    if _DISABLED:
        _STATS["disabled_fallbacks"] += 1
        return candidates[:top_k]

    _STATS["attempts"] += 1
    try:
        response = _call_rerank_judge(query, candidates, judge_model)
        scores = _parse_scores(response, len(candidates))
    except Exception as exc:
        _STATS["failures"] += 1
        _CONSECUTIVE_FAILURES += 1
        if _CONSECUTIVE_FAILURES >= _max_failures_before_disable():
            _DISABLED = True
        logger.warning("LLM rerank failed; using input order: %s", exc)
        return candidates[:top_k]

    if not scores:
        _STATS["failures"] += 1
        _CONSECUTIVE_FAILURES += 1
        if _CONSECUTIVE_FAILURES >= _max_failures_before_disable():
            _DISABLED = True
        logger.warning("LLM rerank returned no parseable scores; using input order")
        return candidates[:top_k]

    _STATS["successes"] += 1
    _CONSECUTIVE_FAILURES = 0
    ranked = sorted(
        enumerate(candidates),
        key=lambda item: (-scores.get(item[0], -1.0), item[0]),
    )
    return [candidate for _, candidate in ranked[:top_k]]
