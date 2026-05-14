from __future__ import annotations

import argparse
import gc
import json
import os
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Any

import requests
from huggingface_hub import hf_hub_download
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memorymaster.models import CitationInput  # noqa: E402
from memorymaster.embeddings import create_best_provider  # noqa: E402
from memorymaster.config import reset_config  # noqa: E402
from memorymaster.service import MemoryService  # noqa: E402


DATA_PATH = ROOT / "benchmark" / "data" / "longmemeval_s_cleaned.json"
RESULTS_OUTPUT = ROOT / "benchmark" / "longmemeval_s_results.json"
QA_OUTPUT = ROOT / "benchmark" / "longmemeval_s_qa.json"
REPO_ID = "xiaowu0162/longmemeval-cleaned"
FILENAME = "longmemeval_s_cleaned.json"
BENCH_SCOPE = "benchmark:longmemeval-s"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_JUDGE_MODEL = "claude-sonnet-4-5"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
GEMINI_GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OPENAI_JUDGE_MODEL = "gpt-4o"
GEMINI_FALLBACK_MODEL = "gemini-2.5-flash"
DEFAULT_JUDGE = "sonnet"
DEFAULT_JUDGE_PACING_SECONDS = 1.0
DEFAULT_QA_MAX_SECONDS = 90 * 60
DEFAULT_CHUNK_CHARS = 0


@dataclass(frozen=True)
class RetrievalResult:
    question_id: str
    question_type: str
    answer_session_ids: list[str]
    top_session_ids: list[str]
    reciprocal_rank: float
    recall_at_5: bool
    recall_at_10: bool
    top_contexts: list[str]


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    provider: str
    tokens: int = 0


@dataclass
class JudgeClient:
    anthropic_api_key: str
    openai_api_key: str
    gemini_api_key: str
    pacing_seconds: float = DEFAULT_JUDGE_PACING_SECONDS
    primary: str = DEFAULT_JUDGE
    total_tokens: int = 0
    models_used: set[str] = field(default_factory=set)
    call_models: list[str] = field(default_factory=list)
    calls: int = 0

    def __post_init__(self) -> None:
        self.provider_order = self._provider_order(self.primary)
        self.active_provider_idx = 0

    def complete(self, prompt: str, *, max_tokens: int, temperature: float = 0.0) -> LLMResponse:
        last_error: Exception | None = None
        while self.active_provider_idx < len(self.provider_order):
            provider = self.provider_order[self.active_provider_idx]
            self._pace()
            try:
                response = self._complete_with_provider(provider, prompt, max_tokens=max_tokens, temperature=temperature)
                self._record(response)
                return response
            except Exception as exc:
                last_error = exc
                if self.active_provider_idx + 1 >= len(self.provider_order):
                    break
                next_provider = self.provider_order[self.active_provider_idx + 1]
                print(
                    f"[judge] {self._provider_label(provider)} failed after retries; "
                    f"switching to {self._provider_label(next_provider)} for the rest of the run: {exc}"
                )
                self.active_provider_idx += 1

        raise RuntimeError("All judge providers failed") from last_error

    def _complete_with_provider(
        self,
        provider: str,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        if provider == "sonnet":
            if not self.anthropic_api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            return call_anthropic_sonnet(
                prompt,
                self.anthropic_api_key,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        if provider == "gemini":
            if not self.gemini_api_key:
                raise RuntimeError("GEMINI_API_KEY is not set")
            return call_gemini(prompt, self.gemini_api_key, max_tokens=max_tokens, temperature=temperature)
        if provider == "gpt-4o":
            if not self.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY is not set")
            return call_openai_chat(prompt, self.openai_api_key, max_tokens=max_tokens, temperature=temperature)
        raise ValueError(f"Unknown judge provider: {provider}")

    def _record(self, response: LLMResponse) -> None:
        self.total_tokens += response.tokens
        self.models_used.add(response.model)
        self.call_models.append(response.model)
        self.calls += 1

    def _pace(self) -> None:
        if self.pacing_seconds > 0:
            time.sleep(self.pacing_seconds)

    @property
    def judge_used_label(self) -> str:
        if not self.models_used:
            return "none"
        if self.models_used == {OPENAI_JUDGE_MODEL}:
            return "gpt-4o"
        if self.models_used == {GEMINI_FALLBACK_MODEL}:
            return "gemini-2.5-flash"
        if self.models_used == {ANTHROPIC_JUDGE_MODEL}:
            return "sonnet"
        return "mixed"

    @staticmethod
    def _provider_order(primary: str) -> list[str]:
        if primary == "sonnet":
            return ["sonnet", "gemini", "gpt-4o"]
        if primary == "gpt-4o":
            return ["gpt-4o", "gemini", "sonnet"]
        if primary == "gemini":
            return ["gemini", "sonnet", "gpt-4o"]
        raise ValueError(f"Unknown judge: {primary}")

    @staticmethod
    def _provider_label(provider: str) -> str:
        if provider == "sonnet":
            return ANTHROPIC_JUDGE_MODEL
        if provider == "gemini":
            return GEMINI_FALLBACK_MODEL
        return OPENAI_JUDGE_MODEL


def ensure_dataset(dataset_path: Path = DATA_PATH) -> Path:
    if dataset_path.exists():
        print(f"[download] using cached {dataset_path}")
        return dataset_path

    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[download] fetching {REPO_ID}/{FILENAME} -> {dataset_path}")
    downloaded = hf_hub_download(
        repo_id=REPO_ID,
        filename=FILENAME,
        repo_type="dataset",
        local_dir=str(dataset_path.parent),
    )
    print(f"[download] ready {downloaded}")
    return Path(downloaded)


def load_dataset(dataset_path: Path = DATA_PATH) -> list[dict[str, Any]]:
    path = ensure_dataset(dataset_path)
    return json.loads(path.read_text(encoding="utf-8"))


def session_to_text(session: list[dict[str, Any]], session_id: str, session_date: str) -> str:
    parts = [f"Session ID: {session_id}", f"Session date: {session_date}"]
    for turn in session:
        role = str(turn.get("role") or "unknown")
        content = str(turn.get("content") or "").strip()
        if content:
            parts.append(f"{role}: {content}")
    return "\n".join(parts)


def chunk_text(text: str, chunk_chars: int = DEFAULT_CHUNK_CHARS) -> list[str]:
    if chunk_chars <= 0 or len(text) <= chunk_chars:
        return [text]
    return [text[idx : idx + chunk_chars] for idx in range(0, len(text), chunk_chars)]


def init_ephemeral_service(tmpdir: Path, embedding_provider: Any | None = None) -> MemoryService:
    db_path = tmpdir / "memorymaster-longmemeval.db"
    old_qdrant = os.environ.pop("QDRANT_URL", None)
    try:
        service = MemoryService(db_target=db_path, workspace_root=tmpdir)
        service.embedding_provider = embedding_provider
        service.init_db()
    finally:
        if old_qdrant is not None:
            os.environ["QDRANT_URL"] = old_qdrant
    return service


def ingest_haystack(service: MemoryService, item: dict[str, Any], *, chunk_chars: int = DEFAULT_CHUNK_CHARS) -> None:
    sessions = item["haystack_sessions"]
    session_ids = item["haystack_session_ids"]
    dates = item.get("haystack_dates") or [""] * len(sessions)
    qid = item["question_id"]

    for idx, session in enumerate(sessions):
        session_id = str(session_ids[idx])
        session_date = str(dates[idx]) if idx < len(dates) else ""
        text = session_to_text(session, session_id, session_date)
        if not text.strip():
            continue
        chunks = chunk_text(text, chunk_chars=chunk_chars)
        for chunk_idx, chunk in enumerate(chunks):
            suffix = f":{chunk_idx}" if len(chunks) > 1 else ""
            service.ingest(
                text=chunk,
                citations=[
                    CitationInput(
                        source=session_id,
                        locator=f"question_id={qid};session_index={idx};chunk={chunk_idx}",
                        excerpt=chunk[:500],
                    )
                ],
                idempotency_key=f"longmemeval:{qid}:{session_id}{suffix}",
                claim_type="fact",
                subject=session_id,
                predicate="conversation_session",
                object_value=session_date,
                scope=BENCH_SCOPE,
                confidence=1.0,
                volatility="low",
                source_agent=session_id,
            )


def query_memory(service: MemoryService, question: str, top_k: int = 10) -> list[dict[str, Any]]:
    service_limit = top_k if llm_rerank_available() else top_k * 3
    return service.query_rows(
        query_text=question,
        limit=service_limit,
        include_candidates=True,
        retrieval_mode="hybrid",
        scope_allowlist=[BENCH_SCOPE],
        allow_sensitive=True,
    )


def llm_rerank_available() -> bool:
    return (
        os.environ.get("MEMORYMASTER_LLM_RERANK", "").strip().lower()
        in {"1", "true", "yes", "on"}
        and bool(os.environ.get("GEMINI_API_KEY", "").strip())
    )


def extract_session_id(row: dict[str, Any]) -> str:
    claim = row["claim"]
    if claim.source_agent:
        return claim.source_agent
    if claim.citations:
        return claim.citations[0].source
    return claim.subject or ""


def unique_by_session(rows: list[dict[str, Any]], top_k: int = 10) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        session_id = extract_session_id(row)
        if session_id in seen:
            continue
        seen.add(session_id)
        unique.append(row)
        if len(unique) >= top_k:
            break
    return unique


def score_retrieval(item: dict[str, Any], rows: list[dict[str, Any]]) -> RetrievalResult:
    rows = unique_by_session(rows, top_k=10)
    top_session_ids = [extract_session_id(row) for row in rows]
    gold = [str(sid) for sid in item.get("answer_session_ids") or []]
    gold_set = set(gold)
    reciprocal_rank = 0.0
    for rank, session_id in enumerate(top_session_ids, start=1):
        if session_id in gold_set:
            reciprocal_rank = 1.0 / rank
            break

    top_contexts = []
    for row in rows[:5]:
        claim = row["claim"]
        top_contexts.append(
            "\n".join(
                [
                    f"session_id: {extract_session_id(row)}",
                    f"score: {row.get('score', 0):.6f}",
                    claim.text[:4000],
                ]
            )
        )

    return RetrievalResult(
        question_id=str(item["question_id"]),
        question_type=str(item.get("question_type") or "unknown"),
        answer_session_ids=gold,
        top_session_ids=top_session_ids,
        reciprocal_rank=reciprocal_rank,
        recall_at_5=bool(gold_set & set(top_session_ids[:5])),
        recall_at_10=bool(gold_set & set(top_session_ids[:10])),
        top_contexts=top_contexts,
    )


def aggregate_retrieval(results: list[RetrievalResult]) -> dict[str, Any]:
    total = len(results)
    if total == 0:
        return {"count": 0, "recall_at_5": 0.0, "recall_at_10": 0.0, "mrr": 0.0}
    return {
        "count": total,
        "recall_at_5": sum(r.recall_at_5 for r in results) / total,
        "recall_at_10": sum(r.recall_at_10 for r in results) / total,
        "mrr": sum(r.reciprocal_rank for r in results) / total,
    }


def run_retrieval(
    data: list[dict[str, Any]],
    *,
    limit: int | None = None,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
) -> tuple[dict[str, Any], list[RetrievalResult]]:
    selected = data[:limit] if limit else data
    results: list[RetrievalResult] = []
    start = time.time()
    os.environ.setdefault("MEMORYMASTER_LLM_RERANK", "1")
    reset_config()
    use_llm_rerank = llm_rerank_available()
    embedding_provider = create_best_provider()
    print(
        f"[retrieval] embedding_provider={embedding_provider.model} "
        f"semantic={embedding_provider.is_semantic} llm_rerank={use_llm_rerank}"
    )

    for idx, item in enumerate(selected, start=1):
        with tempfile.TemporaryDirectory(prefix="mm-longmemeval-", ignore_cleanup_errors=True) as tmp:
            service = init_ephemeral_service(Path(tmp), embedding_provider=embedding_provider)
            try:
                ingest_haystack(service, item, chunk_chars=chunk_chars)
                rows = query_memory(service, str(item["question"]), top_k=10)
                results.append(score_retrieval(item, rows))
            finally:
                del service
                gc.collect()

        if idx % 25 == 0 or idx == len(selected):
            elapsed = time.time() - start
            rate = idx / elapsed if elapsed else 0.0
            print(f"[retrieval] {idx}/{len(selected)} complete ({rate:.2f} q/s)")

    aggregate = aggregate_retrieval(results)
    rerank_stats = {"attempts": 0, "successes": 0, "failures": 0, "disabled": 0}
    if use_llm_rerank:
        from memorymaster.llm_rerank import get_rerank_stats

        rerank_stats = get_rerank_stats()
    payload = {
        "mode": "retrieval-only",
        "dataset": "LongMemEval-S cleaned",
        "questions": len(selected),
        "retrieval_path": (
            "MemoryService.query_rows hybrid lexical+vector ranker over claims "
            f"with embedding_provider={embedding_provider.model}"
            + (" plus Gemini top-50 cross-encoder rerank" if use_llm_rerank else "")
        ),
        "ingest_path": (
            "one MemoryMaster claim per haystack session"
            if chunk_chars <= 0
            else f"MemoryMaster claims chunked at {chunk_chars} chars per haystack session"
        ),
        "metrics": aggregate,
        "llm_rerank": {
            "enabled": use_llm_rerank,
            "model": "gemini-2.5-flash" if use_llm_rerank else "none",
            "approx_calls": int(rerank_stats.get("attempts") or 0),
            "successes": int(rerank_stats.get("successes") or 0),
            "failures": int(rerank_stats.get("failures") or 0),
            "disabled": bool(rerank_stats.get("disabled")),
        },
        "results": [r.__dict__ for r in results],
        "elapsed_seconds": round(time.time() - start, 3),
    }
    print_scores("retrieval", aggregate)
    return payload, results


def retry_after_seconds(response: requests.Response) -> float | None:
    retry_after = response.headers.get("retry-after") or response.headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        return max(0.0, float(retry_after))
    except ValueError:
        return None


def retry_after_from_exception(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    retry_after = headers.get("retry-after") or headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        return max(0.0, float(retry_after))
    except ValueError:
        return None


def is_retryable_llm_exception(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, requests.ConnectionError, requests.Timeout)):
        return True
    if exc.__class__.__name__ in {"APIConnectionError", "APITimeoutError", "RateLimitError"}:
        return True
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    status_code = status_code or getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code == 429 or status_code >= 500
    return isinstance(exc, requests.HTTPError)


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(min=2, max=60),
    retry=retry_if_exception(is_retryable_llm_exception),
    reraise=True,
)
def call_openai_chat(prompt: str, api_key: str, *, max_tokens: int, temperature: float = 0.0) -> LLMResponse:
    payload = {
        "model": OPENAI_JUDGE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    response = requests.post(
        OPENAI_CHAT_URL,
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=60,
    )
    if response.status_code == 429:
        sleep_for = retry_after_seconds(response)
        if sleep_for is not None:
            sleep_for = min(sleep_for, 60.0)
            print(f"[openai] HTTP 429; honoring retry-after={sleep_for:.1f}s")
            time.sleep(sleep_for)
    if response.status_code >= 400:
        body = response.text[:500]
        exc = requests.HTTPError(f"OpenAI HTTP {response.status_code}: {body}", response=response)
        raise exc

    result = response.json()
    usage = result.get("usage") or {}
    return LLMResponse(
        text=str(result["choices"][0]["message"]["content"]).strip(),
        model=OPENAI_JUDGE_MODEL,
        provider="openai",
        tokens=int(usage.get("total_tokens") or 0),
    )


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(min=2, max=60),
    retry=retry_if_exception(is_retryable_llm_exception),
    reraise=True,
)
def call_gemini(prompt: str, api_key: str, *, max_tokens: int, temperature: float = 0.0) -> LLMResponse:
    url = GEMINI_GENERATE_URL.format(model=GEMINI_FALLBACK_MODEL)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    response = requests.post(f"{url}?key={api_key}", json=payload, timeout=60)
    if response.status_code == 429:
        sleep_for = retry_after_seconds(response) or 60.0
        print(f"[gemini] HTTP 429; honoring retry-after={min(sleep_for, 60.0):.1f}s")
        time.sleep(min(sleep_for, 60.0))
    if response.status_code >= 400:
        raise requests.HTTPError(
            f"Gemini HTTP {response.status_code}: {response.text[:500]}",
            response=response,
        )

    result = response.json()
    candidates = result.get("candidates") or []
    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
    text = "".join(str(part.get("text", "")) for part in parts).strip()
    usage = result.get("usageMetadata") or {}
    return LLMResponse(
        text=text,
        model=GEMINI_FALLBACK_MODEL,
        provider="gemini",
        tokens=int(usage.get("totalTokenCount") or 0),
    )


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(min=2, max=60),
    retry=retry_if_exception(is_retryable_llm_exception),
    reraise=True,
)
def call_anthropic_sonnet(prompt: str, api_key: str, *, max_tokens: int, temperature: float = 0.0) -> LLMResponse:
    try:
        return call_anthropic_sonnet_sdk(
            prompt,
            api_key,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except ImportError:
        return call_anthropic_sonnet_requests(
            prompt,
            api_key,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as exc:
        sleep_for = retry_after_from_exception(exc)
        if sleep_for is not None:
            sleep_for = min(sleep_for, 60.0)
            print(f"[anthropic] HTTP 429; honoring retry-after={sleep_for:.1f}s")
            time.sleep(sleep_for)
        raise


def call_anthropic_sonnet_sdk(
    prompt: str,
    api_key: str,
    *,
    max_tokens: int,
    temperature: float = 0.0,
) -> LLMResponse:
    from anthropic import Anthropic

    client = Anthropic(api_key=api_key, timeout=60.0, max_retries=0)
    message = client.messages.create(
        model=ANTHROPIC_JUDGE_MODEL,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(str(block.text) for block in message.content if getattr(block, "type", "") == "text").strip()
    usage = getattr(message, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    return LLMResponse(
        text=text,
        model=ANTHROPIC_JUDGE_MODEL,
        provider="anthropic",
        tokens=input_tokens + output_tokens,
    )


def call_anthropic_sonnet_requests(
    prompt: str,
    api_key: str,
    *,
    max_tokens: int,
    temperature: float = 0.0,
) -> LLMResponse:
    payload = {
        "model": ANTHROPIC_JUDGE_MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    response = requests.post(
        ANTHROPIC_MESSAGES_URL,
        json=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        timeout=60,
    )
    if response.status_code == 429:
        sleep_for = retry_after_seconds(response) or 60.0
        print(f"[anthropic] HTTP 429; honoring retry-after={min(sleep_for, 60.0):.1f}s")
        time.sleep(min(sleep_for, 60.0))
    if response.status_code >= 400:
        raise requests.HTTPError(
            f"Anthropic HTTP {response.status_code}: {response.text[:500]}",
            response=response,
        )

    result = response.json()
    text = "".join(str(block.get("text", "")) for block in result.get("content", [])).strip()
    usage = result.get("usage") or {}
    tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    return LLMResponse(
        text=text,
        model=ANTHROPIC_JUDGE_MODEL,
        provider="anthropic",
        tokens=tokens,
    )


def answer_question(question: str, contexts: list[str], judge: JudgeClient) -> str:
    prompt = "\n\n".join(
        [
            "Answer the question using only the retrieved conversation context.",
            "If the context does not contain the answer, say I don't know.",
            "Keep the answer concise.",
            "Retrieved context:",
            "\n\n---\n\n".join(contexts),
            f"Question: {question}",
        ]
    )
    return judge.complete(prompt, max_tokens=300, temperature=0.0).text


def judge_answer(gold: str, hypothesis: str, judge: JudgeClient) -> str:
    prompt = (
        "Given the gold answer X and the hypothesis Y, is Y correct? Reply YES or NO.\n\n"
        f"X: {gold}\n\n"
        f"Y: {hypothesis}"
    )
    verdict = judge.complete(prompt, max_tokens=5, temperature=0.0).text.upper()
    return "YES" if verdict.startswith("YES") else "NO"


def run_full(
    data: list[dict[str, Any]],
    retrieval_results: list[RetrievalResult],
    *,
    limit: int | None = None,
    judge_name: str = DEFAULT_JUDGE,
    judge_pacing_seconds: float = DEFAULT_JUDGE_PACING_SECONDS,
    max_seconds: int | None = DEFAULT_QA_MAX_SECONDS,
    mode: str = "full",
) -> dict[str, Any]:
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not anthropic_key and not openai_key and not gemini_key:
        print("[full] no ANTHROPIC_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY; deferring QA")
        return {
            "mode": mode,
            "status": "deferred-2",
            "questions": 0,
            "requested_questions": len(data[:limit] if limit else data),
            "accuracy": None,
            "correct": 0,
            "by_question_type": {},
            "results": [],
            "elapsed_seconds": 0.0,
            "judge_model": "none",
            "judge_primary": judge_name,
            "tokens": 0,
        }

    selected = data[:limit] if limit else data
    by_id = {result.question_id: result for result in retrieval_results}
    details = []
    by_type: dict[str, Counter[str]] = defaultdict(Counter)
    judge = JudgeClient(
        anthropic_api_key=anthropic_key,
        openai_api_key=openai_key,
        gemini_api_key=gemini_key,
        pacing_seconds=judge_pacing_seconds,
        primary=judge_name,
    )
    started = time.time()

    for idx, item in enumerate(selected, start=1):
        if max_seconds is not None and time.time() - started >= max_seconds:
            print(f"[full] stopping at {len(details)} completed questions after {max_seconds}s budget")
            break
        qid = str(item["question_id"])
        retrieval = by_id[qid]
        call_start = len(judge.call_models)
        try:
            hypothesis = answer_question(str(item["question"]), retrieval.top_contexts, judge)
            verdict = judge_answer(str(item["answer"]), hypothesis, judge)
        except Exception as exc:
            print(f"[full] stopping after {len(details)} completed questions: {exc}")
            break
        correct = verdict == "YES"
        qtype = str(item.get("question_type") or "unknown")
        by_type[qtype]["total"] += 1
        by_type[qtype]["correct"] += int(correct)
        details.append(
            {
                "question_id": qid,
                "question_type": qtype,
                "answer": item["answer"],
                "hypothesis": hypothesis,
                "verdict": verdict,
                "correct": correct,
                "judge_models": judge.call_models[call_start:],
                "top_session_ids": retrieval.top_session_ids,
                "answer_session_ids": retrieval.answer_session_ids,
            }
        )

        if idx % 10 == 0 or idx == len(selected):
            accuracy = sum(d["correct"] for d in details) / len(details)
            print(
                f"[full] {idx}/{len(selected)} complete "
                f"accuracy={accuracy:.4f} judge={judge.judge_used_label} tokens={judge.total_tokens}"
            )

    total = len(details)
    correct_total = sum(d["correct"] for d in details)
    breakdown = {
        qtype: {
            "count": counts["total"],
            "correct": counts["correct"],
            "accuracy": counts["correct"] / counts["total"] if counts["total"] else 0.0,
        }
        for qtype, counts in sorted(by_type.items())
    }
    status = "complete" if total == len(selected) else "partial" if total else "deferred-2"
    payload = {
        "mode": mode,
        "judge_model": judge.judge_used_label,
        "judge_primary": judge_name,
        "judge_retry_policy": (
            "Selected primary judge uses tenacity: 5 attempts, exponential backoff 2-60s, "
            "429 retry-after honored; sonnet default cascades Sonnet -> Gemini -> GPT-4o"
        ),
        "judge_pacing_seconds": judge_pacing_seconds,
        "status": status,
        "questions": total,
        "requested_questions": len(selected),
        "accuracy": correct_total / total if total else None,
        "correct": correct_total,
        "by_question_type": breakdown,
        "results": details,
        "elapsed_seconds": round(time.time() - started, 3),
        "tokens": judge.total_tokens,
    }
    if total:
        print(f"[full] accuracy={payload['accuracy']:.4f} correct={correct_total}/{total}")
    print(f"[full] judge={payload['judge_model']} tokens={judge.total_tokens}")
    return payload


def load_retrieval_payload(results_path: Path = RESULTS_OUTPUT) -> tuple[dict[str, Any], list[RetrievalResult]]:
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    retrieval_payload = payload["retrieval"]
    retrieval_results = [
        RetrievalResult(
            question_id=str(row["question_id"]),
            question_type=str(row.get("question_type") or "unknown"),
            answer_session_ids=[str(sid) for sid in row.get("answer_session_ids", [])],
            top_session_ids=[str(sid) for sid in row.get("top_session_ids", [])],
            reciprocal_rank=float(row.get("reciprocal_rank") or 0.0),
            recall_at_5=bool(row.get("recall_at_5")),
            recall_at_10=bool(row.get("recall_at_10")),
            top_contexts=[str(ctx) for ctx in row.get("top_contexts", [])[:5]],
        )
        for row in retrieval_payload["results"]
    ]
    return retrieval_payload, retrieval_results


def write_results(
    retrieval_payload: dict[str, Any],
    full_payload: dict[str, Any] | None,
    *,
    output_path: Path = RESULTS_OUTPUT,
) -> dict[str, Any]:
    payload = {
        "dataset": retrieval_payload["dataset"],
        "questions": retrieval_payload["questions"],
        "status": full_payload.get("status") if full_payload else "retrieval-only",
        "retrieval": retrieval_payload,
        "qa": full_payload,
        "elapsed_seconds": round(
            retrieval_payload.get("elapsed_seconds", 0.0)
            + ((full_payload or {}).get("elapsed_seconds") or 0.0),
            3,
        ),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[results] wrote {output_path}")
    return payload


def write_qa_results(qa_payload: dict[str, Any], *, output_path: Path = QA_OUTPUT) -> dict[str, Any]:
    payload = {
        "dataset": "LongMemEval-S cleaned",
        "status": qa_payload.get("status"),
        "qa": qa_payload,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[qa] wrote {output_path}")
    return payload


def print_scores(label: str, metrics: dict[str, Any]) -> None:
    print(
        f"[{label}] count={metrics['count']} "
        f"R@5={metrics['recall_at_5']:.4f} "
        f"R@10={metrics['recall_at_10']:.4f} "
        f"MRR={metrics['mrr']:.4f}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MemoryMaster LongMemEval-S benchmark harness")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--retrieval-only", action="store_true", help="Run retrieval metrics only")
    mode.add_argument("--full", action="store_true", help="Run retrieval plus answer/judge pass")
    mode.add_argument("--qa-only", action="store_true", help="Run answer/judge pass using existing retrieval results")
    parser.add_argument("--limit", type=int, default=None, help="Optional question limit for partial runs")
    parser.add_argument("--dataset", type=Path, default=DATA_PATH, help="Dataset JSON path")
    parser.add_argument("--output", type=Path, default=RESULTS_OUTPUT, help="Results JSON path")
    parser.add_argument("--qa-output", type=Path, default=QA_OUTPUT, help="QA-only output JSON path")
    parser.add_argument(
        "--judge",
        choices=["sonnet", "gpt-4o", "gemini"],
        default=DEFAULT_JUDGE,
        help="Primary judge model; default sonnet",
    )
    parser.add_argument(
        "--judge-pacing-seconds",
        type=float,
        default=DEFAULT_JUDGE_PACING_SECONDS,
        help="Delay between judge model calls; default 1.0s",
    )
    parser.add_argument(
        "--qa-max-seconds",
        type=int,
        default=DEFAULT_QA_MAX_SECONDS,
        help="Maximum QA runtime before saving partial results; default 5400",
    )
    parser.add_argument(
        "--chunk-chars",
        type=int,
        default=DEFAULT_CHUNK_CHARS,
        help="Chunk haystack sessions at this character count before ingest; 0 keeps one claim per session",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data = load_dataset(args.dataset)
    if args.qa_only:
        retrieval_payload, retrieval_results = load_retrieval_payload(args.output)
        full_payload = run_full(
            data,
            retrieval_results,
            limit=args.limit,
            judge_name=args.judge,
            judge_pacing_seconds=args.judge_pacing_seconds,
            max_seconds=args.qa_max_seconds,
            mode="qa-only",
        )
        output = write_qa_results(full_payload, output_path=args.qa_output)
        metrics = retrieval_payload["metrics"]
        qa = output.get("qa") or {}
    else:
        retrieval_payload, retrieval_results = run_retrieval(data, limit=args.limit, chunk_chars=args.chunk_chars)
        full_payload = None
        if args.full:
            full_payload = run_full(
                data,
                retrieval_results,
                limit=args.limit,
                judge_name=args.judge,
                judge_pacing_seconds=args.judge_pacing_seconds,
                max_seconds=args.qa_max_seconds,
            )
        output = write_results(retrieval_payload, full_payload, output_path=args.output)
        metrics = output["retrieval"]["metrics"]
        qa = output.get("qa") or {}
    qa_accuracy = qa.get("accuracy")
    print(f"RESULT_R5={metrics['recall_at_5']:.4f}")
    print(f"RESULT_R10={metrics['recall_at_10']:.4f}")
    print(f"RESULT_MRR={metrics['mrr']:.4f}")
    print(f"RESULT_QA_ACCURACY={qa_accuracy:.4f}" if qa_accuracy is not None else "RESULT_QA_ACCURACY=deferred-2")
    print(f"RESULT_QUESTIONS_RUN={metrics['count']} out of {len(data)}")
    print(f"RESULT_JUDGE_USED={qa.get('judge_model') or 'none'}")
    rerank_meta = (output.get("retrieval") or retrieval_payload).get("llm_rerank") or {}
    print(f"RESULT_GEMINI_CALLS={int(rerank_meta.get('approx_calls') or 0)}")


if __name__ == "__main__":
    main()
