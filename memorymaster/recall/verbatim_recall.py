"""MemPalace-style verbatim retrieval — third candidate stream after FTS5+entity.

Design
------
`memorymaster.recall.verbatim_store` already stores raw conversation turns in the
`verbatim_memories` table with an FTS5 virtual table (`verbatim_fts`). The
stop-hook fills it on every session end (see ``store_transcript``). That
corpus is completely untouched by the claims-based recall path.

This module adds an opt-in third retrieval stream:

* Tokenize the query via ``recall_tokenizer._candidate_tokens`` (same
  stopword/length filter as the claims pipeline — keeps results consistent).
* AND-join tokens inside a quoted FTS5 MATCH over ``verbatim_fts``.
* Score with the absolute value of FTS5 ``rank`` (smaller rank = better hit;
  we flip sign so bigger is better downstream).
* Return lightweight records the ranker can mix in as *synthetic candidates*
  with ``source="verbatim"``. The hook stores them alongside claim rows and
  weights them with a new, default-zero ``W_VERBATIM`` knob.

Environment toggles
-------------------
* ``MEMORYMASTER_RECALL_VERBATIM=1`` — enable the stream. Default off so no
  behaviour changes when the env var is absent.
* ``MEMORYMASTER_RECALL_W_VERBATIM=<float>`` — weight applied to the
  ``verbatim_score`` field in the final re-rank. Default 0.0; the shipped
  recommendation after the eval below is either 0.0 (null result) or the
  value documented in ``artifacts/verbatim-recall-eval-2026-04-23.md``.

Read-only against the DB — we only SELECT from ``verbatim_memories`` and
``verbatim_fts``. A missing table is treated as "verbatim disabled" so this
module never breaks legacy DBs.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass

from memorymaster.recall.recall_tokenizer import _candidate_tokens

logger = logging.getLogger(__name__)

# Shipped default: OFF. Flip the env var to 1 to opt in.
_ENABLE_ENV = "MEMORYMASTER_RECALL_VERBATIM"
_WEIGHT_ENV = "MEMORYMASTER_RECALL_W_VERBATIM"
_WEIGHT_DEFAULT = 0.0

# Content-length cap for the excerpt we surface downstream — long verbatim
# rows blow the hook's token budget. Full content stays in the DB.
_EXCERPT_CHARS = 300

# Minimum token length we accept from the tokenizer. Matches the claims
# pipeline (``_candidate_tokens`` already enforces length>=3 via _MIN=3).
_MIN_TOKEN_LEN = 3


@dataclass(frozen=True)
class VerbatimHit:
    """Lightweight result row for verbatim recall."""
    verbatim_id: int
    scope: str
    excerpt: str
    score: float
    session_id: str
    role: str


def is_enabled() -> bool:
    """True iff MEMORYMASTER_RECALL_VERBATIM is set to a truthy value."""
    raw = os.environ.get(_ENABLE_ENV, "0").strip()
    return raw not in ("", "0", "false", "False", "no", "off")


def verbatim_weight() -> float:
    """Read the W_VERBATIM weight from env, falling back to the default."""
    raw = os.environ.get(_WEIGHT_ENV)
    if raw is None or raw.strip() == "":
        return _WEIGHT_DEFAULT
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, falling back to %.2f",
                       _WEIGHT_ENV, raw, _WEIGHT_DEFAULT)
        return _WEIGHT_DEFAULT


def _escape_fts5_token(tok: str) -> str:
    """Quote a single token for safe inclusion inside FTS5 MATCH.

    FTS5 treats double-quotes as phrase delimiters and handles embedded
    double-quotes by doubling them. Non-alphanumerics (``-``, ``/``, ``_``)
    are fine inside a quoted phrase.
    """
    return '"' + tok.replace('"', '""') + '"'


def _verbatim_table_exists(conn: sqlite3.Connection) -> bool:
    """True iff both ``verbatim_memories`` and ``verbatim_fts`` are present."""
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type IN ('table', 'view') "
            "  AND name IN ('verbatim_memories', 'verbatim_fts')"
        ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("verbatim_recall: sqlite_master scan failed: %s", exc)
        return False
    names = {r[0] for r in rows}
    return "verbatim_memories" in names and "verbatim_fts" in names


def _build_match_expr(query: str) -> str:
    """Produce a safe FTS5 MATCH expression from a raw query.

    Tokenizes via ``recall_tokenizer._candidate_tokens`` (shares the
    stopword/stem filter with the claims pipeline), deduplicates while
    preserving order, then AND-joins quoted tokens. Returns "" if no
    usable tokens survive filtering — caller must treat that as "no
    verbatim stream".
    """
    tokens = _candidate_tokens(query or "")
    uniq: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        if len(tok) < _MIN_TOKEN_LEN:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        uniq.append(tok)
    if not uniq:
        return ""
    # Cap the number of tokens per query. FTS5 AND queries with >6 terms
    # almost never hit, even for well-formed prompts — mirrors the
    # ``max_tokens=6`` cap used by extract_query_tokens.
    uniq = uniq[:6]
    return " AND ".join(_escape_fts5_token(t) for t in uniq)


def recall_verbatim(
    query: str,
    scope: str | None,
    db_path: str,
    limit: int = 5,
) -> list[VerbatimHit]:
    """Return up to ``limit`` verbatim rows matching ``query``.

    Parameters
    ----------
    query:
        Raw user prompt. Tokenized internally — caller does NOT need to
        pre-process.
    scope:
        Optional scope filter (matches ``verbatim_memories.scope`` with a
        ``LIKE scope%`` prefix, consistent with the existing
        ``verbatim_store._search_fts`` contract). Pass ``None`` to search
        across all scopes.
    db_path:
        Path to the memorymaster DB. Opened read-only.
    limit:
        Maximum number of rows to return. Capped at 20 defensively.

    Returns
    -------
    list[VerbatimHit]
        Empty list when:
        * query tokenizes to nothing,
        * verbatim tables are absent from the DB,
        * no rows match,
        * the query fails (any SQLite error is swallowed — verbatim is
          best-effort; it must never break the hook).
    """
    if not query or not query.strip():
        return []
    limit = max(1, min(limit, 20))
    match_expr = _build_match_expr(query)
    if not match_expr:
        return []

    # Read-only open so we never accidentally write from the hook path.
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.debug("verbatim_recall: cannot open %s ro: %s", db_path, exc)
        return []

    try:
        if not _verbatim_table_exists(conn):
            return []
        conn.row_factory = sqlite3.Row
        if scope:
            rows = conn.execute(
                """
                SELECT v.id, v.session_id, v.role, v.content, v.scope, rank AS score
                  FROM verbatim_fts f
                  JOIN verbatim_memories v ON v.id = f.rowid
                 WHERE verbatim_fts MATCH ? AND v.scope LIKE ?
                 ORDER BY rank
                 LIMIT ?
                """,
                (match_expr, f"{scope}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT v.id, v.session_id, v.role, v.content, v.scope, rank AS score
                  FROM verbatim_fts f
                  JOIN verbatim_memories v ON v.id = f.rowid
                 WHERE verbatim_fts MATCH ?
                 ORDER BY rank
                 LIMIT ?
                """,
                (match_expr, limit),
            ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("verbatim_recall: query failed: %s", exc)
        return []
    finally:
        conn.close()

    out: list[VerbatimHit] = []
    for r in rows:
        raw_score = r["score"] if r["score"] is not None else 0.0
        # FTS5 returns negative numbers where smaller == better match.
        # Flip sign so bigger == better (ranker convention).
        normalised = abs(float(raw_score))
        content = r["content"] or ""
        excerpt = content[:_EXCERPT_CHARS]
        out.append(
            VerbatimHit(
                verbatim_id=int(r["id"]),
                scope=str(r["scope"] or ""),
                excerpt=excerpt,
                score=normalised,
                session_id=str(r["session_id"] or ""),
                role=str(r["role"] or ""),
            )
        )
    return out


def hit_to_synthetic_row(hit: VerbatimHit) -> dict:
    """Convert a ``VerbatimHit`` into a query_rows-shaped synthetic candidate.

    The ranker in ``context_hook.recall`` reads ``claim.text``, ``claim.id``
    and the score fields. We fabricate a minimal claim-like object with the
    attributes the downstream ranker touches. ``verbatim_score`` is set to
    the (positive-oriented) FTS5 score; every other score field is zero so
    weights other than ``W_VERBATIM`` contribute nothing.
    """
    # Lazy import — avoids circularity at module load and keeps verbatim
    # a leaf module.
    from memorymaster.models import Claim

    # ID namespace: negative to avoid collision with real claim.id values
    # in downstream dedupe sets. Ranker only uses id for de-dup; any
    # negative int outside the claims space is safe.
    synthetic_id = -(hit.verbatim_id + 1)
    claim = Claim(
        id=synthetic_id,
        text=hit.excerpt,
        idempotency_key=None,
        normalized_text=None,
        claim_type="verbatim",
        subject=None,
        predicate=None,
        object_value=None,
        scope=hit.scope or "project",
        volatility="low",
        status="confirmed",
        confidence=0.0,
        pinned=False,
        supersedes_claim_id=None,
        replaced_by_claim_id=None,
        created_at="",
        updated_at="",
        last_validated_at=None,
        archived_at=None,
        wiki_article=None,
    )
    return {
        "claim": claim,
        "status": "confirmed",
        "annotation": None,
        "score": 0.0,
        "lexical_score": 0.0,
        "freshness_score": 0.0,
        "confidence_score": 0.0,
        "vector_score": 0.0,
        "entity_score": 0.0,
        "verbatim_score": hit.score,
        "source": "verbatim",
        "_verbatim_id": hit.verbatim_id,
    }
