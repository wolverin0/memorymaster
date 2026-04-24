"""Query expansion via entity-matched synonyms (roadmap 1.5).

Given a raw user query, extract entities via :func:`entity_extractor.extract_patterns`,
resolve each entity to its top-2 most-common aliases in the ``entity_aliases``
table, and return the original query plus those alias tokens so the FTS5 stage
can widen its search via ``OR`` boolean clauses.

Fully opt-in: the wiring in :mod:`context_hook` only calls this when
``MEMORYMASTER_RECALL_QUERY_EXPANSION=1``. When the feature is off, or the
entity tables are missing, or the DB is unreachable, the caller keeps its
original behaviour bit-for-bit.

Algorithm (per spec):
    1. Extract entities from the query via ``extract_patterns``.
    2. For each entity, look up ``entity_aliases`` rows matching the entity's
       normalized alias and pick the top-2 most-common aliases (ranked by
       alias count — a proxy for "how often has this surface form shown up").
    3. Return ``[original_query, *alias_tokens]`` with duplicates removed and
       case normalized where it helps.

Public API: :func:`expand_query`.
"""
from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

__all__ = ["expand_query", "ALIASES_PER_ENTITY", "MAX_TOTAL_ALIASES"]

# How many alias tokens to pull per extracted entity. Kept small so the OR
# expansion never blows up the FTS5 query cost — two aliases per entity on a
# typical prompt with 2-3 entities yields 4-6 extra tokens.
ALIASES_PER_ENTITY = 2

# Hard cap on total alias tokens across all entities, to guard against
# pathological prompts (10+ entities) that could make the FTS5 OR clause
# explode. Matches the spirit of the _ENTITY_CAP_TOTAL budget in
# context_hook._entity_fanout_claim_ids.
MAX_TOTAL_ALIASES = 8


def _top_aliases_for_entity(
    conn: sqlite3.Connection,
    alias_key: str,
    limit: int,
) -> list[str]:
    """Return the top-``limit`` most-common alias forms for the entity whose
    normalized alias matches ``alias_key``.

    "Most-common" is measured by the number of ``entity_aliases`` rows that
    share the same ``alias`` (normalized lookup key) — which is the count of
    distinct surface-form variants we've recorded for that alias. This is a
    reasonable proxy for salience: an entity like ``qdrant`` accumulates
    variants (``Qdrant``, ``QDRANT``, ``qdrant-cloud``) as more claims
    mention it, so its alias count climbs.

    Ties broken by ``original_form`` length (shorter wins) then alphabetical.
    Returns lowercased original_forms so callers can dedupe against query
    tokens that are also lowercased. Never raises — missing tables or DB
    errors return an empty list so the caller degrades gracefully.
    """
    if not alias_key or limit <= 0:
        return []

    try:
        # First: resolve entity_id(s) matching the normalized alias.
        rows = conn.execute(
            "SELECT DISTINCT entity_id FROM entity_aliases WHERE alias = ?",
            (alias_key,),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("query_expansion: alias lookup failed: %s", exc)
        return []

    entity_ids = [int(r[0]) for r in rows if r and r[0] is not None]
    if not entity_ids:
        return []

    out: list[str] = []
    seen_normalized: set[str] = set()
    # Pull original_form variants for each resolved entity. A single alias
    # key may resolve to multiple entity_ids on legacy DBs — process each,
    # respecting the per-entity limit.
    placeholders = ",".join("?" for _ in entity_ids)
    try:
        # GROUP BY original_form counts occurrences (proxy for popularity).
        query = (
            f"SELECT LOWER(original_form) AS form, COUNT(*) AS cnt "
            f"FROM entity_aliases "
            f"WHERE entity_id IN ({placeholders}) AND original_form IS NOT NULL "
            f"GROUP BY LOWER(original_form) "
            f"ORDER BY cnt DESC, LENGTH(original_form) ASC, form ASC"
        )
        variant_rows = conn.execute(query, entity_ids).fetchall()
    except sqlite3.Error as exc:
        logger.debug("query_expansion: variant fetch failed: %s", exc)
        return []

    for form, _cnt in variant_rows:
        if not form:
            continue
        tok = str(form).strip().lower()
        if not tok or tok in seen_normalized:
            continue
        seen_normalized.add(tok)
        out.append(tok)
        if len(out) >= limit:
            break
    return out


def expand_query(query: str, conn: sqlite3.Connection) -> list[str]:
    """Return ``[query, *alias_tokens]`` — the original query followed by
    top-N alias variants for each entity extracted from it.

    Args:
        query: raw user query (any length, any language). An empty or
            whitespace-only query short-circuits to ``[query]``.
        conn: open SQLite connection. MUST have read permission on
            ``entity_aliases``; missing tables cause graceful degradation
            (returns ``[query]``).

    Returns:
        A list starting with ``query`` and followed by 0 or more alias
        tokens. Aliases are lowercased and deduped against each other; the
        caller is responsible for deduping against query tokens if needed.
        Never returns an empty list — even a zero-entity query yields
        ``[query]`` so callers can always splat the result.
    """
    if not isinstance(query, str):
        return [query]
    if not query.strip():
        return [query]

    # Lazy import — avoids a circular dependency with context_hook and keeps
    # the import cost off the hot path for callers that never enable this
    # feature.
    try:
        from memorymaster.entity_extractor import extract_patterns
        from memorymaster.entity_registry import normalize_alias
    except Exception as exc:  # pragma: no cover — import failure rare
        logger.debug("query_expansion: import skipped: %s", exc)
        return [query]

    entities = extract_patterns(query)
    if not entities:
        return [query]

    # Dedupe by normalized alias so two entities that collapse to the same
    # alias form don't consume double the budget.
    alias_keys: list[str] = []
    seen_keys: set[str] = set()
    for ent in entities:
        key = normalize_alias(ent.canonical_hint)
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        alias_keys.append(key)

    if not alias_keys:
        return [query]

    query_lower = query.lower()
    expansions: list[str] = []
    emitted: set[str] = set()
    for key in alias_keys:
        if len(expansions) >= MAX_TOTAL_ALIASES:
            break
        per_entity = _top_aliases_for_entity(
            conn, key, ALIASES_PER_ENTITY
        )
        for tok in per_entity:
            # Don't re-emit tokens that literally appear inside the original
            # query — they'd be redundant OR clauses.
            if tok in emitted or tok in query_lower:
                continue
            emitted.add(tok)
            expansions.append(tok)
            if len(expansions) >= MAX_TOTAL_ALIASES:
                break

    return [query, *expansions]
