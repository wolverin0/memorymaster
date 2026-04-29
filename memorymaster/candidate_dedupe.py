"""Pre-steward candidate dedupe (v3.13).

Checks each `candidate` claim against existing claims in the same scope BEFORE
invoking the steward LLM. Candidates that score above a configurable Jaccard
threshold are flagged for archive; the steward applies the SQL transition and
skips its LLM call.

Two-stage:
1. FTS5 OR-query narrows candidates by lexical overlap (cheap top-K filter).
2. Token-set Jaccard scores the final match (corpus-independent, predictable).

We use Jaccard rather than raw BM25 because BM25 in SQLite FTS5 collapses to
near-zero on small corpora (IDF goes to ~0 when most tokens appear in every
doc). Jaccard works the same on a 2-doc fixture and on a 20k-claim DB.

Env flags:
  MEMORYMASTER_DEDUPE_ENABLED      default "0" (off)
  MEMORYMASTER_DEDUPE_SHADOW       default "1" (count would-archive but don't act)
  MEMORYMASTER_DEDUPE_JACCARD_HIGH default "0.85"
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Literal

DedupeAction = Literal["archive", "passthrough"]


@dataclass(frozen=True)
class DedupeResult:
    action: DedupeAction
    canonical_claim_id: int | None
    jaccard_score: float | None
    reason: str


_DEFAULT_JACCARD_HIGH = 0.85
_TRUTHY = {"1", "true", "yes", "on", "y"}
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")
_FTS_TOP_K = 5


def is_enabled() -> bool:
    return os.getenv("MEMORYMASTER_DEDUPE_ENABLED", "0").strip().lower() in _TRUTHY


def is_shadow_mode() -> bool:
    return os.getenv("MEMORYMASTER_DEDUPE_SHADOW", "1").strip().lower() in _TRUTHY


def jaccard_high_threshold() -> float:
    raw = os.getenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", str(_DEFAULT_JACCARD_HIGH))
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_JACCARD_HIGH


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def jaccard_tokens(a: str, b: str) -> float:
    """Return |A ∩ B| / |A ∪ B| over case-folded word tokens."""
    set_a = _tokenize(a)
    set_b = _tokenize(b)
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union else 0.0


def _escape_fts5_query(text: str) -> str:
    """Build an OR-joined FTS5 query so matches don't require every token."""
    tokens = _TOKEN_RE.findall(text.lower())
    if not tokens:
        return '""'
    escaped = ['"' + token.replace('"', '""') + '"' for token in tokens]
    return " OR ".join(escaped)


def _has_fts5_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims_fts'"
    ).fetchone()
    return row is not None


def fts_candidates_in_scope(
    conn: sqlite3.Connection,
    *,
    scope: str,
    text: str,
    exclude_id: int,
    limit: int = _FTS_TOP_K,
) -> list[tuple[int, str, str]]:
    """Return list of (id, text, status) candidate matches via FTS5 OR-query.

    Empty list if FTS5 isn't present, scope is empty, or there are no matches.
    Excludes archived claims and the candidate itself.
    """
    if not text or not text.strip() or not scope:
        return []
    if not _has_fts5_table(conn):
        return []

    fts_query = _escape_fts5_query(text)
    rows = conn.execute(
        """
        SELECT c.id, c.text, c.status
        FROM claims c
        JOIN claims_fts ON claims_fts.rowid = c.id
        WHERE claims_fts MATCH ?
          AND c.scope = ?
          AND c.id <> ?
          AND c.status <> 'archived'
        ORDER BY bm25(claims_fts) ASC
        LIMIT ?
        """,
        (fts_query, scope, exclude_id, limit),
    ).fetchall()

    return [(int(r[0]), r[1] or "", r[2] or "") for r in rows]


def find_near_duplicate(
    conn: sqlite3.Connection,
    *,
    candidate_id: int,
    candidate_text: str,
    candidate_scope: str,
    jaccard_high: float | None = None,
) -> DedupeResult:
    """Decide whether a candidate is a near-duplicate of an existing claim.

    Pulls top-K BM25 matches in the same scope, then scores each with token
    Jaccard. Returns archive action with the best canonical match if its
    Jaccard score is >= jaccard_high; otherwise passthrough.

    Caller honors shadow mode — this function never mutates state.
    """
    threshold = jaccard_high if jaccard_high is not None else jaccard_high_threshold()

    if not candidate_text or len(candidate_text.strip()) < 10:
        return DedupeResult(
            action="passthrough",
            canonical_claim_id=None,
            jaccard_score=None,
            reason="text-too-short",
        )

    matches = fts_candidates_in_scope(
        conn,
        scope=candidate_scope,
        text=candidate_text,
        exclude_id=candidate_id,
        limit=_FTS_TOP_K,
    )
    if not matches:
        return DedupeResult(
            action="passthrough",
            canonical_claim_id=None,
            jaccard_score=None,
            reason="no-fts-matches",
        )

    best_id: int | None = None
    best_score = 0.0
    best_status = ""
    for cid, ctext, cstatus in matches:
        score = jaccard_tokens(candidate_text, ctext)
        if score > best_score:
            best_score = score
            best_id = cid
            best_status = cstatus

    if best_score >= threshold and best_id is not None:
        return DedupeResult(
            action="archive",
            canonical_claim_id=best_id,
            jaccard_score=best_score,
            reason=f"jaccard>={threshold:.2f} canonical-status={best_status}",
        )

    return DedupeResult(
        action="passthrough",
        canonical_claim_id=best_id,
        jaccard_score=best_score,
        reason=f"jaccard<{threshold:.2f}",
    )
