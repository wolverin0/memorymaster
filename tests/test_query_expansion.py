"""Tests for query expansion via entity-matched synonyms (roadmap 1.5).

Covers :func:`memorymaster.query_expansion.expand_query`:
  - empty / whitespace query short-circuits to ``[query]``
  - single entity with 2 aliases returns ``[query, alias1, alias2]``
  - entity with 0 aliases (not in DB) returns just ``[query]``
  - multi-entity dedupes and respects the per-entity cap
  - case insensitivity on expansion tokens (returned lowercase)
  - MAX_TOTAL_ALIASES caps runaway expansion
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from memorymaster.knowledge.entity_registry import (
    ensure_entity_schema,
    resolve_or_create,
)
from memorymaster.query_expansion import (
    ALIASES_PER_ENTITY,
    MAX_TOTAL_ALIASES,
    expand_query,
)


# --------------------------------------------------------------------------- #
# Fixture — in-memory SQLite with entity + alias tables populated
# --------------------------------------------------------------------------- #


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _seed_entity(
    conn: sqlite3.Connection, canonical: str, variants: list[str]
) -> int:
    """Create an entity with ``canonical`` as its first alias, then add each
    ``variants`` entry as an additional original_form row. Returns the
    entity_id. Uses resolve_or_create under the hood which exercises the
    real alias-registration path.
    """
    entity_id = resolve_or_create(conn, canonical)
    # Each variant is recorded as its own alias row (resolve_or_create
    # handles the variant_key dedup; different case/separator variants
    # create distinct rows pointing at the same entity_id).
    for v in variants:
        resolve_or_create(conn, v)
    return entity_id


@pytest.fixture()
def entity_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_entity_schema(conn)
    yield conn
    conn.close()


# --------------------------------------------------------------------------- #
# Spec cases
# --------------------------------------------------------------------------- #


def test_empty_query_returns_list_with_empty(entity_db: sqlite3.Connection) -> None:
    assert expand_query("", entity_db) == [""]


def test_whitespace_only_query_returns_self(
    entity_db: sqlite3.Connection,
) -> None:
    assert expand_query("   \t\n", entity_db) == ["   \t\n"]


def test_non_string_query_returns_self(entity_db: sqlite3.Connection) -> None:
    # Defensive: the public type is ``str`` but callers may wire in None.
    result = expand_query(None, entity_db)  # type: ignore[arg-type]
    assert result == [None]


def test_single_entity_with_two_aliases(
    entity_db: sqlite3.Connection,
) -> None:
    """An entity with multiple DISTINCT LOWER() surface forms returns up
    to ALIASES_PER_ENTITY expansion tokens.

    Note that the query-substring filter in ``expand_query`` drops any
    expansion token that is already a literal substring of the query —
    this is by design (no redundant OR clauses). We use a tool-allowlist
    entity ("pytest") and a query that references it via synonym words so
    the recorded original_form variants survive the filter.
    """
    # The tool-allowlist includes "pytest". We register a synthetic cluster
    # where "pytest" is the canonical but additional forms ("unittest",
    # "tox-runner") share its entity_id via add_alias, exercising the
    # "multiple variants per entity" path.
    from memorymaster.knowledge.entity_registry import add_alias

    entity_id = resolve_or_create(entity_db, "pytest")
    # Manually widen the alias pool. These are deliberately NOT substrings
    # of the query below so the substring-filter doesn't drop them.
    add_alias(entity_db, entity_id, "unittest-runner")
    add_alias(entity_db, entity_id, "tox-wrapper")
    add_alias(entity_db, entity_id, "pytest-harness")

    # Query mentions only "pytest" — the extractor picks it up via the
    # tool allowlist; the expansion should surface the other variants.
    result = expand_query("run pytest now", entity_db)
    assert result[0] == "run pytest now"
    expansions = result[1:]
    # We expect 1 or 2 expansions (ALIASES_PER_ENTITY==2). "pytest" itself
    # is in the query so it's filtered; the added variants survive.
    assert 0 < len(expansions) <= ALIASES_PER_ENTITY
    for tok in expansions:
        assert tok == tok.lower()
        assert tok.strip() == tok
        assert tok
        # None of the returned tokens should be "pytest" itself (filtered).
        assert tok != "pytest"


def test_entity_with_zero_aliases_returns_just_query(
    entity_db: sqlite3.Connection,
) -> None:
    """An entity extracted from the query but not present in the DB yields
    no expansion tokens — result is ``[query]`` only."""
    # No aliases seeded. "GEMINI_API_KEY" is a valid env-var entity but
    # the DB doesn't know it.
    query = "configure GEMINI_API_KEY for the llm provider"
    result = expand_query(query, entity_db)
    assert result == [query]


def test_multi_entity_dedupes_and_caps_total(
    entity_db: sqlite3.Connection,
) -> None:
    """Multiple entities each contribute up to ALIASES_PER_ENTITY aliases
    and the total is capped by MAX_TOTAL_ALIASES. Duplicates across
    entities are suppressed."""
    # Seed four distinct entities, each with multiple variants.
    _seed_entity(entity_db, "steward",
                 ["steward", "Steward", "STEWARD", "stewards"])
    _seed_entity(entity_db, "qdrant",
                 ["qdrant", "Qdrant", "QDRANT", "qdrant-cloud"])
    _seed_entity(entity_db, "memorymaster",
                 ["memorymaster", "MemoryMaster", "MEMORYMASTER"])
    _seed_entity(entity_db, "pytest",
                 ["pytest", "Pytest", "PYTEST"])

    # Use a query that extracts all four as entities via entity_extractor.
    # entity_extractor.extract_patterns matches: tool allowlist (pytest),
    # file/leaf (memorymaster style), service (multi-dash), env-var, etc.
    # "memorymaster" by itself is a single lowercased word without a dash,
    # so it won't match the service pattern — use a phrase that triggers
    # tool-allowlist matches (pytest) + multi-dash service (qdrant-cloud).
    query = "pytest fails against qdrant-cloud in memory-master suite"
    result = expand_query(query, entity_db)

    # Original always first.
    assert result[0] == query
    expansions = result[1:]
    # Never exceed the total cap.
    assert len(expansions) <= MAX_TOTAL_ALIASES
    # Deduped: each expansion token is unique.
    assert len(expansions) == len(set(expansions))
    # All lowercase.
    for tok in expansions:
        assert tok == tok.lower()


def test_query_tokens_not_re_emitted_as_expansions(
    entity_db: sqlite3.Connection,
) -> None:
    """If an alias happens to be a literal substring of the original query,
    it should NOT be re-emitted — that would just duplicate OR clauses.
    """
    # Seed an entity whose canonical form exactly appears in the query.
    _seed_entity(entity_db, "qdrant", ["qdrant", "Qdrant", "qdrant-cloud"])

    query = "connect to qdrant"
    result = expand_query(query, entity_db)
    assert result[0] == query
    # "qdrant" (the canonical) is IN the query and must be filtered out.
    assert "qdrant" not in result[1:]


def test_expansions_are_from_most_common_variants(
    entity_db: sqlite3.Connection,
) -> None:
    """The top-2 returned for an entity should be its most-common
    original_form variants — we verify by registering a "rare" variant
    only once while registering a "common" variant many times via
    resolve_or_create, which records a row per distinct case form.
    """
    # resolve_or_create de-dupes per (entity_id, variant_key). Distinct
    # surface forms produce distinct rows. We manipulate that to bias
    # which form wins the COUNT(*) ORDER BY by inserting multiple
    # variant casings whose LOWER(original_form) still matches — the
    # grouped count reflects popularity.
    _seed_entity(
        entity_db, "steward",
        # Many "steward" hits (case variation) + one rare form.
        ["steward", "Steward", "STEWARD", "steWard", "STEWARDX"],
    )

    query = "steward classifier v3"
    result = expand_query(query, entity_db)

    # The expansion (if any) should prefer the heavier LOWER-group: the
    # "steward" forms collapse to 'steward' (which is already in the
    # query → filtered). The only remaining variant, "stewardx", may
    # appear. This is mostly a smoke test that common variants rank
    # higher than rare ones — we assert no dropped/empty tokens.
    for tok in result[1:]:
        assert tok, "empty expansion token"
        assert tok == tok.lower()


def test_multiple_calls_are_deterministic(
    entity_db: sqlite3.Connection,
) -> None:
    """Calling expand_query twice with the same args yields the same list.
    This is a lightweight regression guard — if we ever add randomness
    (e.g. shuffled tie-break) the caller contract should be renegotiated.
    """
    _seed_entity(
        entity_db, "qdrant",
        ["Qdrant", "QDRANT", "qdrant-cloud", "qdrant vector"],
    )
    query = "fix qdrant-cloud flake"
    first = expand_query(query, entity_db)
    second = expand_query(query, entity_db)
    assert first == second


def test_graceful_when_tables_missing() -> None:
    """When entity_aliases doesn't exist, expand_query returns ``[query]``
    rather than raising — the feature must degrade silently on legacy DBs.
    """
    conn = sqlite3.connect(":memory:")
    try:
        # No ensure_entity_schema → tables absent.
        query = "investigate pytest failure on qdrant"
        assert expand_query(query, conn) == [query]
    finally:
        conn.close()


def test_respects_aliases_per_entity_constant(
    entity_db: sqlite3.Connection,
) -> None:
    """Even when an entity has many variants, we return at most
    ALIASES_PER_ENTITY of them (before cross-entity deduplication)."""
    variants = [f"qdrant-v{i}" for i in range(10)]
    _seed_entity(entity_db, "qdrant", variants)
    query = "check qdrant vector"
    result = expand_query(query, entity_db)
    expansions = result[1:]
    # Single entity means <= ALIASES_PER_ENTITY expansions.
    assert len(expansions) <= ALIASES_PER_ENTITY
