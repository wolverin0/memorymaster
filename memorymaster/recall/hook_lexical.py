"""Prompt-hook lexical helpers (T-1862, external review F1/F4/F8).

* whole-token term coverage: a query term matches a claim only as a whole (normalised) token, never as a
  substring ("port" does not match "report"); the hook's row gate is built on it and does not read confidence;
* truth eligibility: a claim whose replacement is pending or explicit never ranks above its correction in
  what the hook finally injects, whichever ranker (lexical or dense) produced the order.

Standard library plus the recall tokenizer; every failure degrades to "no effect".
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from typing import Any

from memorymaster.recall.recall_tokenizer import _candidate_tokens

logger = logging.getLogger(__name__)

MIN_TERMS_ENV = "MEMORYMASTER_RECALL_HOOK_MIN_TERMS"
DEFAULT_MIN_TERMS = 2  # distinct whole query terms a row must contain (fewer when the question has fewer)


def min_terms() -> int:
    try:
        return max(1, int(os.environ.get(MIN_TERMS_ENV, DEFAULT_MIN_TERMS)))
    except ValueError:
        return DEFAULT_MIN_TERMS


def term_key(token: str) -> str:
    """Whole-token comparison key: lower case, a plural/verb ending and a trailing ``e`` folded."""
    tok = token.lower()
    if not tok.endswith("ss"):
        for suffix in ("ing", "ed", "es", "s"):
            if tok.endswith(suffix) and len(tok) - len(suffix) >= 3:
                tok = tok[: -len(suffix)]
                break
    return tok[:-1] if tok.endswith("e") and len(tok) > 3 else tok


def term_keys(terms: Iterable[str]) -> frozenset[str]:
    return frozenset(term_key(t) for t in terms)


def text_keys(*texts: object) -> frozenset[str]:
    """Whole-token keys of the given texts (non-strings ignored)."""
    out: set[str] = set()
    for text in texts:
        if isinstance(text, str):
            out.update(term_key(t) for t in _candidate_tokens(text))
    return frozenset(out)


def matched_terms(keys: frozenset[str], *texts: object) -> frozenset[str]:
    """The query term keys that occur as whole tokens in ``texts``."""
    return keys & text_keys(*texts)


def covers(matched: frozenset[str], total_terms: int, identifiers: frozenset[str]) -> bool:
    """The coverage rule: enough distinct terms matched, or one exact identifier matched.

    ``total_terms`` is the number of distinct query terms, ``identifiers`` their identifier keys.
    A question without terms gates nothing.
    """
    if total_terms <= 0:
        return True
    return len(matched) >= min(min_terms(), total_terms) or bool(matched & identifiers)


# --- truth eligibility (F4) ---------------------------------------------------------------------


def _disputed(pool: list[Any], svc: Any) -> tuple[set[int], set[int]]:
    """``(disputed ids, ids whose correction is in the pool)`` among ``pool`` (rows of claims)."""
    from memorymaster.recall.retrieval import pending_supersession_ids

    claims = [row.get("claim") for row in pool]
    ids = {int(c.id) for c in claims if isinstance(getattr(c, "id", None), int)}
    superseded_by_pool: set[int] = set()
    for claim in claims:
        target = getattr(claim, "supersedes_claim_id", None)
        if isinstance(target, int):
            superseded_by_pool.add(target)  # the correction itself is in the pool: it names its predecessor
    for claim in claims:
        replacement = getattr(claim, "replaced_by_claim_id", None)
        if isinstance(getattr(claim, "id", None), int) and isinstance(replacement, int) and replacement in ids:
            superseded_by_pool.add(int(claim.id))
    pending = pending_supersession_ids(svc)
    return (set(pending) | superseded_by_pool) & ids, superseded_by_pool & ids


def apply_truth_eligibility(rows: list[dict], svc: Any) -> list[dict]:
    """Final-stage truth check: disputed claims never precede their correction.

    A claim is disputed when its supersession is proposed and unresolved (``pending_supersession_ids``), when a
    row of the pool names it in ``supersedes_claim_id`` or when its ``replaced_by_claim_id`` is in the pool.
    A disputed claim whose correction is in the pool is dropped; one whose correction is not retrieved only
    sinks below every undisputed row. Never raises: on any fault the order is returned unchanged.
    """
    try:
        disputed, has_correction = _disputed(rows, svc)
        if not disputed:
            return rows
        keep = [r for r in rows if getattr(r.get("claim"), "id", None) not in has_correction]
        undisputed = [r for r in keep if getattr(r.get("claim"), "id", None) not in disputed]
        return undisputed + [r for r in keep if getattr(r.get("claim"), "id", None) in disputed]
    except Exception as exc:  # noqa: BLE001 - truth eligibility must never break recall
        logger.debug("truth eligibility skipped: %s", type(exc).__name__)
        return rows
