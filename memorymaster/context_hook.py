"""Context hook — automatic memory extraction and injection for Claude Code.

Two functions:
  1. recall(query) — query memorymaster for relevant context before responding
  2. observe(text, source) — extract and ingest claims after a conversation turn

Designed to be called from Claude Code hooks or CLAUDE.md instructions.

Usage (CLI):
    memorymaster recall "what is the user working on?"
    memorymaster observe --text "User decided to use PostgreSQL" --source "session"
    memorymaster observe --stdin < conversation_turn.txt --source "session"

Usage (from CLAUDE.md):
    Before responding, run: memorymaster recall "<user message summary>"
    After important decisions: memorymaster observe --text "<decision>" --source "session"
"""

from __future__ import annotations

import logging
import math
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# BM25 lexical re-scorer (ships on by default after the 5x5 k1/b sweep
# on 30-prompt eval — see artifacts/bm25-sweep-2026-04-23.md).
#
# Beats the previous overlap-based `_lexical_score` by +0.113 p@5 and
# +0.108 MAP@5 on the 30-prompt eval with non-empty rate held at 28/30.
# k1=1.2, b=0.25 are the shipped defaults (tied with six other combos
# at p@5=0.393; picked because they are classical-BM25 canonical values
# and maximise MAP@5 across ties). Override via env:
#     MEMORYMASTER_BM25_K1=<float>
#     MEMORYMASTER_BM25_B=<float>
#     MEMORYMASTER_LEXICAL_BM25=0           # disable, fall back to overlap scorer
_BM25_K1_DEFAULT = 1.2
_BM25_B_DEFAULT = 0.25


def _bm25_param(name: str, default: float) -> float:
    raw = os.environ.get(f"MEMORYMASTER_BM25_{name}")
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid MEMORYMASTER_BM25_%s=%r, falling back to %.2f",
                       name, raw, default)
        return default


def _bm25_enabled() -> bool:
    raw = os.environ.get("MEMORYMASTER_LEXICAL_BM25", "1").strip()
    return raw not in ("0", "false", "False", "no", "off", "")


# Recall re-ranker weights (8 dims, matches scripts/eval_recall_precision_at_5.py).
# Baseline (w0) held after autoresearch candidate #4 grid search on
# artifacts/real-prompts.jsonl (30 prompts) — grid winner (+0.02 p@5 at
# hook-matched top_k=8) also regressed MAP@5 by -0.006, so baseline wins.
# Override any single weight via env var, e.g.:
#     MEMORYMASTER_RECALL_W_FRESHNESS=0.15
#     MEMORYMASTER_RECALL_W_ENTITY=0.15
# See artifacts/eval/recall-precision-grid-k8-mov1.jsonl for the full grid.
#
# W_ENTITY (dim 8) powers the entity-link fanout stage. When set to 0.0
# (default), the fanout only runs as a rescue path — i.e. when the FTS5
# stage returned zero hits. Whenever FTS5 produced >=1 hit, the fanout is
# skipped entirely, so the top-K ranking is bit-identical to pre-fanout
# behaviour. Set W_ENTITY > 0 to also run fanout after a non-empty FTS5
# stage and let entity-matched claims compete in the ranker.
_RECALL_WEIGHT_DEFAULTS: dict[str, float] = {
    "W_MATCHES": 0.3,
    "W_PHRASE": 0.3,
    "W_ALL": 0.2,
    "W_LEXICAL": 0.1,
    "W_CONFIDENCE": 0.1,
    "W_FRESHNESS": 0.0,
    "W_VECTOR": 0.0,
    "W_ENTITY": 0.0,
}


def _recall_weight(name: str) -> float:
    """Read a single recall-ranker weight from env, falling back to default."""
    env_key = f"MEMORYMASTER_RECALL_{name}"
    raw = os.environ.get(env_key)
    if raw is None or raw.strip() == "":
        return _RECALL_WEIGHT_DEFAULTS[name]
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, falling back to default %.2f",
                       env_key, raw, _RECALL_WEIGHT_DEFAULTS[name])
        return _RECALL_WEIGHT_DEFAULTS[name]


# Per-call fanout caps. Kept conservative so a pathological prompt (10+ env-vars)
# doesn't blow up the hook budget: at most _ENTITY_CAP_PER_ENTITY claims per
# matched entity, at most _ENTITY_CAP_TOTAL new claims added overall.
_ENTITY_CAP_PER_ENTITY = 3
_ENTITY_CAP_TOTAL = 8


def _entity_fanout_claim_ids(
    store,
    prompt: str,
    seen_ids: set[int],
) -> list[int]:
    """Mine entities from the prompt, resolve to entity_ids via entity_aliases,
    and return claim IDs where ``claims.entity_id`` matches — excluding IDs
    already seen by the FTS5 stage.

    Best-effort: any DB error returns an empty list so the fanout never
    breaks the recall hook. The tables ``entities`` / ``entity_aliases`` are
    created lazily by ``ensure_entity_schema`` at ingest time, so we tolerate
    their absence on legacy DBs.
    """
    try:
        from memorymaster.entity_extractor import extract_patterns
        from memorymaster.entity_registry import normalize_alias
    except Exception:  # pragma: no cover — import errors are fatal elsewhere
        return []

    entities = extract_patterns(prompt or "")
    if not entities:
        return []

    # Dedupe by normalized alias (entity_extractor already dedupes by
    # canonical_hint, but different kinds can collapse to the same alias form
    # — e.g. "git" as tool vs "git" substring of something else).
    aliases: list[str] = []
    seen_aliases: set[str] = set()
    for ent in entities:
        alias = normalize_alias(ent.canonical_hint)
        if not alias or alias in seen_aliases:
            continue
        seen_aliases.add(alias)
        aliases.append(alias)

    if not aliases:
        return []

    new_ids: list[int] = []
    try:
        with store.connect() as conn:
            # One SELECT per alias so per-entity cap is enforceable
            # without a correlated subquery. Aliases are indexed, so this
            # is cheap even with 10 entities.
            for alias in aliases:
                if len(new_ids) >= _ENTITY_CAP_TOTAL:
                    break
                rows = conn.execute(
                    """
                    SELECT DISTINCT c.id
                      FROM entity_aliases a
                      JOIN claims c ON c.entity_id = a.entity_id
                     WHERE a.alias = ?
                       AND c.status != 'archived'
                       AND (c.visibility IS NULL OR c.visibility = 'public')
                     ORDER BY c.updated_at DESC
                     LIMIT ?
                    """,
                    (alias, _ENTITY_CAP_PER_ENTITY),
                ).fetchall()
                for row in rows:
                    cid = int(row[0])
                    if cid in seen_ids:
                        continue
                    seen_ids.add(cid)
                    new_ids.append(cid)
                    if len(new_ids) >= _ENTITY_CAP_TOTAL:
                        break
    except Exception as exc:
        logger.debug("entity fanout skipped: %s", exc)
        return []

    return new_ids


def _row_for_claim(claim) -> dict:
    """Build a query_rows-shaped row dict for a fanout-sourced claim.

    Scores default to zero so the claim adds no baseline signal; the
    W_ENTITY weight on the ``entity_score`` bit is what promotes it.
    """
    return {
        "claim": claim,
        "status": getattr(claim, "status", "confirmed"),
        "annotation": None,
        "score": 0.0,
        "lexical_score": 0.0,
        "freshness_score": 0.0,
        "confidence_score": float(getattr(claim, "confidence", 0.0) or 0.0),
        "vector_score": 0.0,
        "entity_score": 1.0,
        "source": "entity_fanout",
    }


def _row_for_vector_hit(claim, vector_score: float) -> dict:
    """Build a query_rows-shaped row dict for a Qdrant-sourced claim.

    ``vector_score`` is the raw Qdrant cosine similarity in [0, 1] (values
    below ``MEMORYMASTER_RECALL_VECTOR_SCORE_THRESHOLD`` are filtered out
    upstream). All other signals default to zero so ``W_VECTOR`` is the
    only thing promoting the row — at ``W_VECTOR=0`` (legacy default)
    these rows still add nothing to the ranking.
    """
    return {
        "claim": claim,
        "status": getattr(claim, "status", "confirmed"),
        "annotation": None,
        "score": 0.0,
        "lexical_score": 0.0,
        "freshness_score": 0.0,
        "confidence_score": float(getattr(claim, "confidence", 0.0) or 0.0),
        "vector_score": float(vector_score),
        "entity_score": 0.0,
        "source": "vector_fallback",
    }


def _apply_vector_fallback(
    svc,
    query: str,
    rows: list,
    seen_ids: set[int],
) -> list:
    """Augment ``rows`` with Qdrant semantic-search hits when the primary
    retrieval stages under-produced.

    Triggers only when ``len(rows) < MEMORYMASTER_RECALL_VECTOR_MIN_CANDIDATES``
    (default 3) and every env-var gate is satisfied. Silently degrades on
    any failure (qdrant unreachable, collection missing, embedder import
    error, etc) so the caller keeps whatever FTS5 + entity fanout produced.

    Returns the (possibly augmented) row list. Always mutates ``seen_ids``
    when new rows are added.
    """
    try:
        from memorymaster import qdrant_recall_fallback
    except Exception as exc:  # pragma: no cover — import errors rare
        logger.debug("vector fallback: module import skipped: %s", exc)
        return rows

    if not qdrant_recall_fallback.is_fallback_enabled():
        return rows
    if len(rows) >= qdrant_recall_fallback.fallback_threshold():
        return rows

    try:
        hits = qdrant_recall_fallback.search(query)
    except Exception as exc:  # pragma: no cover — search() already swallows
        logger.debug("vector fallback: search skipped: %s", exc)
        return rows

    if not hits:
        return rows

    # Lazy security check — mirrors the entity fanout treatment.
    try:
        from memorymaster.security import is_sensitive_claim
    except Exception:
        is_sensitive_claim = lambda _claim: False  # type: ignore[assignment]  # noqa: E731

    appended = 0
    for hit in hits:
        cid = hit.claim_id
        if cid in seen_ids:
            continue
        try:
            claim = svc.store.get_claim(cid, include_citations=True)
        except Exception as exc:
            logger.debug("vector fallback: get_claim(%d) failed: %s", cid, exc)
            continue
        if claim is None or getattr(claim, "status", "") == "archived":
            continue
        if is_sensitive_claim(claim):
            continue
        rows.append(_row_for_vector_hit(claim, hit.score))
        seen_ids.add(cid)
        appended += 1

    if appended:
        logger.debug(
            "vector fallback: appended %d rows (total=%d) for query=%r",
            appended, len(rows), query[:60],
        )
    return rows


# Patterns that indicate something worth remembering
OBSERVATION_PATTERNS = [
    # User corrections/preferences
    (r"\b(don'?t|never|always|stop|instead|prefer|please)\b.*", "preference"),
    # Decisions
    (r"\b(decided|decision|we('ll| will)|let'?s|going to|plan is)\b.*", "decision"),
    # Constraints
    (r"\b(must|require|rule|constraint|forbidden|mandatory|critical)\b.*", "constraint"),
    # Architecture/tech choices
    (r"\b(using|switched to|migrated|deployed|installed|configured)\b.*", "fact"),
    # Bug/issue patterns
    (r"\b(bug|fix|broke|crash|error|issue|problem|wrong)\b.*", "event"),
    # Commitments
    (r"\b(todo|will do|next step|action item|need to|should)\b.*", "commitment"),
]

_COMPILED_PATTERNS = [(re.compile(p, re.IGNORECASE), t) for p, t in OBSERVATION_PATTERNS]


def classify_observation(text: str) -> str | None:
    """Check if text contains something worth remembering. Returns claim_type or None."""
    for pattern, claim_type in _COMPILED_PATTERNS:
        if pattern.search(text):
            return claim_type
    return None


def recall(
    query: str,
    *,
    db_path: str = "",
    budget: int = 2000,
    format: str = "text",
    skip_qdrant: bool = False,
) -> str:
    """Query memorymaster for relevant context with quality ranking."""
    from memorymaster.service import MemoryService

    db = db_path or os.environ.get("MEMORYMASTER_DEFAULT_DB") or "memorymaster.db"
    svc = MemoryService(db_target=db, workspace_root=Path.cwd())

    # Pre-extract salient tokens before hitting FTS5. Passing the full
    # prompt verbatim AND-joins every token in FTS5 and rejects nearly all
    # real conversational prompts (see artifacts/retrieval-eval-2026-04-22).
    # FTS5 _escape_fts5_query() quotes-and-AND-joins tokens, so we instead
    # run one query per top token and union the results — effectively OR.
    from memorymaster.recall_tokenizer import extract_query_tokens

    fts_query = extract_query_tokens(query, db, max_tokens=6)
    token_list = fts_query.split() if fts_query else []

    rows: list = []
    seen_ids: set[int] = set()
    if token_list:
        # Fan out: top token first (highest IDF), then widen by OR.
        per_token_limit = max(3, 8 // max(1, len(token_list)))
        for tok in token_list:
            batch = svc.query_rows(
                query_text=tok,
                limit=per_token_limit,
                retrieval_mode="legacy",
                include_candidates=True,
                scope_allowlist=None,
            )
            for row in batch:
                claim = row.get("claim")
                cid = getattr(claim, "id", None)
                if cid is None or cid in seen_ids:
                    continue
                seen_ids.add(cid)
                rows.append(row)
            if len(rows) >= 8:
                break

    if not rows:
        # Fallback to raw prompt — preserves the old behaviour.
        rows = svc.query_rows(
            query_text=query,
            limit=8,
            retrieval_mode="legacy",
            include_candidates=True,
            scope_allowlist=None,
        )
        for row in rows:
            claim = row.get("claim")
            cid = getattr(claim, "id", None)
            if cid is not None:
                seen_ids.add(cid)

    # Entity-link fanout — mine entities from the prompt, resolve via
    # entity_aliases, and union in claims we haven't already seen.
    #
    # Backwards-compat contract: when MEMORYMASTER_RECALL_W_ENTITY == 0.0
    # (shipped default) the fanout ONLY runs if the FTS5 stage returned
    # nothing — it acts purely as a rescue path for zero-hit prompts, which
    # keeps ranking bit-identical for the 24/30 prompts that already hit.
    # When W_ENTITY > 0, fanout runs unconditionally and its rows (with
    # entity_score=1.0, other scores zeroed) contribute to the re-rank.
    w_entity_probe = _recall_weight("W_ENTITY")
    should_fanout = (not rows) or (w_entity_probe > 0.0)
    if should_fanout:
        # Lazy import so legacy callers without the security module still
        # work — fanout is a best-effort layer.
        try:
            from memorymaster.security import is_sensitive_claim
        except Exception:
            is_sensitive_claim = lambda _claim: False  # type: ignore[assignment]  # noqa: E731
        fanout_ids = _entity_fanout_claim_ids(svc.store, query, seen_ids)
        for cid in fanout_ids:
            try:
                claim = svc.store.get_claim(cid, include_citations=True)
            except Exception:
                continue
            if claim is None or getattr(claim, "status", "") == "archived":
                continue
            if is_sensitive_claim(claim):
                continue
            rows.append(_row_for_claim(claim))

    # Vector fallback — Qdrant semantic search when FTS5 + entity fanout
    # produced fewer than MEMORYMASTER_RECALL_VECTOR_MIN_CANDIDATES rows
    # (default 3). Fully env-gated so default behaviour is unchanged. See
    # ``_apply_vector_fallback`` for the exact gating logic.
    rows = _apply_vector_fallback(svc, query, rows, seen_ids)

    if not rows and not skip_qdrant:
        # Fallback to Qdrant semantic search
        try:
            from memorymaster.qdrant_backend import QdrantBackend
            backend = QdrantBackend()
            hits = backend.search(query, limit=5)
            backend.close()
            if hits:
                lines = ["# Memory Context (semantic)", ""]
                for hit in hits:
                    p = hit.get("payload", {})
                    text = p.get("claim_text", "")[:200]
                    lines.append(f"- {text}")
                return "\n".join(lines).encode("ascii", errors="replace").decode("ascii")
        except Exception:
            pass
        return ""

    if not rows:
        return ""

    # Re-rank by lexical relevance — claims with more query words score higher.
    # Use the tokenized query (same terms we actually sent to FTS5) so the
    # post-ranker agrees with retrieval.
    query_words = set(fts_query.lower().split()) or set(query.lower().split())

    # Resolve weights once per call — env overrides shipped defaults.
    w_matches = _recall_weight("W_MATCHES")
    w_phrase = _recall_weight("W_PHRASE")
    w_all = _recall_weight("W_ALL")
    w_lexical = _recall_weight("W_LEXICAL")
    w_confidence = _recall_weight("W_CONFIDENCE")
    w_freshness = _recall_weight("W_FRESHNESS")
    w_vector = _recall_weight("W_VECTOR")
    w_entity = _recall_weight("W_ENTITY")

    # Build BM25 corpus stats over the candidate set once per call. This is
    # cheap (O(rows * avg_doc_len)) and strictly read-only — we never touch
    # the DB past what query_rows already fetched. Feature-flagged; on by
    # default after the sweep. See module-level comment for why.
    bm25_on = _bm25_enabled()
    bm25_scores: dict[int, float] = {}
    if bm25_on:
        from memorymaster.recall_tokenizer import _candidate_tokens

        def _doc_tokens(claim_obj) -> list[str]:
            subject = getattr(claim_obj, "subject", "") or ""
            text = getattr(claim_obj, "text", "") or ""
            joined = f"{subject} {text}"
            return [t for t in _candidate_tokens(joined) if len(t) >= 3]

        # Cache tokenisation per row (keyed by id) and build df + dl stats.
        doc_tokens_by_id: dict[int, list[str]] = {}
        df: dict[str, int] = {}
        for r in rows:
            c = r.get("claim")
            cid = getattr(c, "id", None)
            if cid is None or cid in doc_tokens_by_id:
                continue
            toks = _doc_tokens(c)
            doc_tokens_by_id[cid] = toks
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        n_docs = len(doc_tokens_by_id)
        avg_dl = (
            sum(len(v) for v in doc_tokens_by_id.values()) / n_docs
            if n_docs else 0.0
        )

        k1 = _bm25_param("K1", _BM25_K1_DEFAULT)
        b = _bm25_param("B", _BM25_B_DEFAULT)

        # Query tokens for BM25: use the same tokenizer as the tokenizer
        # pipeline so we agree with the retrieval stage. Fall back to the
        # raw query_words split when the tokenizer finds nothing.
        q_tokens = [t for t in _candidate_tokens(query) if len(t) >= 3]
        if not q_tokens:
            q_tokens = [w for w in query_words if len(w) >= 3]

        if n_docs > 0 and avg_dl > 0 and q_tokens:
            for cid, doc_tokens in doc_tokens_by_id.items():
                if not doc_tokens:
                    continue
                tf: dict[str, int] = {}
                for t in doc_tokens:
                    tf[t] = tf.get(t, 0) + 1
                dl = len(doc_tokens)
                score = 0.0
                for qt in q_tokens:
                    f = tf.get(qt, 0)
                    if f == 0:
                        continue
                    n_q = df.get(qt, 0)
                    idf = math.log(
                        ((n_docs - n_q + 0.5) / (n_q + 0.5)) + 1.0
                    )
                    norm = 1.0 - b + b * (dl / avg_dl)
                    score += idf * ((f * (k1 + 1.0)) / (f + k1 * norm))
                bm25_scores[cid] = score

    def _relevance(row):
        claim = row.get("claim")
        text = (claim.text if hasattr(claim, "text") else "").lower()
        # Count how many query words (length > 2) appear in the claim text.
        tokens_gt2 = [w for w in query_words if len(w) > 2]
        matches = sum(1 for w in tokens_gt2 if w in text)
        # Bonus: full query phrase appears in text.
        phrase_bonus = 1.0 if query.lower() in text else 0.0
        # Bonus: ALL query tokens present (not just some).
        all_present = 1.0 if tokens_gt2 and matches == len(tokens_gt2) else 0.0
        if bm25_on:
            cid = getattr(claim, "id", None)
            lexical = bm25_scores.get(cid, 0.0) if cid is not None else 0.0
        else:
            lexical = float(row.get("lexical_score") or 0.0)
        conf = float(row.get("confidence_score") or 0.0)
        freshness = float(row.get("freshness_score") or 0.0)
        vector = float(row.get("vector_score") or 0.0)
        # entity_score is 1.0 for fanout-sourced claims, absent (→0.0) for
        # FTS5-sourced rows. When W_ENTITY==0.0 this contributes nothing,
        # preserving bit-identical ranking with the pre-fanout implementation.
        entity = float(row.get("entity_score") or 0.0)
        return (
            matches * w_matches
            + phrase_bonus * w_phrase
            + all_present * w_all
            + lexical * w_lexical
            + conf * w_confidence
            + freshness * w_freshness
            + vector * w_vector
            + entity * w_entity
        )

    ranked = sorted(rows, key=_relevance, reverse=True)

    # Build output — top claims within budget
    lines = ["# Memory Context", ""]
    tokens_used = 0
    chars_per_token = 4
    for row in ranked:
        claim = row.get("claim")
        if not hasattr(claim, "text"):
            continue
        text = claim.text[:300]
        wiki_slug = getattr(claim, "wiki_article", None)
        if wiki_slug:
            chunk = f"- {text}  (compiled in [[{wiki_slug}]])"
        else:
            chunk = f"- {text}"
        chunk_tokens = len(chunk) // chars_per_token
        if tokens_used + chunk_tokens > budget:
            break
        lines.append(chunk)
        tokens_used += chunk_tokens

    if len(lines) <= 2:
        return ""

    return "\n".join(lines).encode("ascii", errors="replace").decode("ascii")


def observe(
    text: str,
    *,
    source: str = "session",
    db_path: str = "",
    scope: str = "project",
    auto_classify: bool = True,
    force: bool = False,
) -> dict:
    """Extract and ingest observations from conversation text.

    If auto_classify=True, only ingests text that matches observation patterns.
    If force=True, ingests regardless of pattern matching.

    Returns: {"ingested": bool, "claim_type": str, "claim_id": int | None}
    """
    # Check if worth remembering
    claim_type = None
    if auto_classify:
        claim_type = classify_observation(text)
        if claim_type is None and not force:
            return {"ingested": False, "claim_type": None, "claim_id": None, "reason": "no_pattern_match"}

    if not claim_type:
        claim_type = "fact"

    from memorymaster.models import CitationInput
    from memorymaster.service import MemoryService

    db = db_path or os.environ.get("MEMORYMASTER_DEFAULT_DB") or "memorymaster.db"
    svc = MemoryService(db_target=db, workspace_root=Path.cwd())

    try:
        claim = svc.ingest(
            text=text.strip()[:2000],
            citations=[CitationInput(source=source)],
            claim_type=claim_type,
            scope=scope,
            confidence=0.6,
            source_agent="context-hook",
        )
        return {"ingested": True, "claim_type": claim_type, "claim_id": claim.id}
    except Exception as exc:
        logger.warning("Observe failed: %s", exc)
        return {"ingested": False, "claim_type": claim_type, "claim_id": None, "reason": str(exc)}


def observe_llm(
    text: str,
    *,
    source: str = "session",
    db_path: str = "",
    scope: str = "project",
) -> dict:
    """Use LLM to extract structured claims from conversation text.

    More thorough than rule-based observe() but slower (~5s per call).
    """
    from memorymaster.auto_extractor import extract_claims_from_text
    from memorymaster.models import CitationInput
    from memorymaster.service import MemoryService

    db = db_path or os.environ.get("MEMORYMASTER_DEFAULT_DB") or "memorymaster.db"

    extracted = extract_claims_from_text(text, source=source, scope=scope)
    if not extracted:
        return {"ingested": 0, "extracted": 0}

    svc = MemoryService(db_target=db, workspace_root=Path.cwd())
    ingested = 0
    for claim_data in extracted:
        try:
            svc.ingest(
                text=claim_data.get("text", ""),
                citations=[CitationInput(source=source)],
                claim_type=claim_data.get("claim_type", "fact"),
                subject=claim_data.get("subject"),
                predicate=claim_data.get("predicate"),
                object_value=claim_data.get("object_value"),
                scope=scope,
                confidence=0.6,
                source_agent="context-hook-llm",
            )
            ingested += 1
        except Exception:
            pass

    return {"ingested": ingested, "extracted": len(extracted)}
