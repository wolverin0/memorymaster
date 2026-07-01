"""Tests for the guarded fuzzy entity resolver (anti-hallucination).

WHY this feature exists: ``resolve_or_create`` collapses subject strings onto
canonical entities by EXACT normalized-alias match. A memory system that
also tries *fuzzy* matching is one bad guess away from silently merging two
distinct entities ("MemoryMaster" and "MemoryMonitor") — corrupting the
knowledge graph in a way no later read can detect. The guard makes the fuzzy
path SAFE: it matches only when there is exactly one confident candidate, and
REFUSES (creates nothing, returns 0) the moment two entities tie. The whole
feature is gated behind ``MEMORYMASTER_ENTITY_FUZZY_RESOLVE`` and is OFF by
default, so the released "every distinct form is its own entity" behavior is
byte-identical unless an operator opts in.

Each test encodes the invariant it protects, not just the mechanics.
"""
from __future__ import annotations

import sqlite3
import threading

from memorymaster.knowledge.entity_registry import (
    ensure_entity_schema,
    resolve_or_create,
)

_FLAG = "MEMORYMASTER_ENTITY_FUZZY_RESOLVE"


def _fresh_db() -> sqlite3.Connection:
    """In-memory DB with the claims + entity tables wired up (mirrors the
    fixture in test_entity_registry.py)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE claims (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "subject TEXT, entity_id INTEGER)"
    )
    ensure_entity_schema(conn)
    return conn


def _entity_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]


# --------------------------------------------------------------------------- #
# 1. OFF-by-default invariant — the released behavior must be untouched.
# --------------------------------------------------------------------------- #


def test_off_by_default_near_match_creates_two_entities(monkeypatch):
    """WHY: the feature is recall-altering, so absent the env flag a near-miss
    subject MUST still become its own entity. If this regresses, every shipped
    DB silently starts merging entities on upgrade — unacceptable for a
    released package.
    """
    monkeypatch.delenv(_FLAG, raising=False)
    conn = _fresh_db()
    id1 = resolve_or_create(conn, "MemoryMaster")
    id2 = resolve_or_create(conn, "memory-master")  # near, but distinct norm form
    assert id1 > 0 and id2 > 0
    assert id1 != id2, "fuzzy resolve leaked into the default (flag-off) path"
    assert _entity_count(conn) == 2


# --------------------------------------------------------------------------- #
# 2. Unambiguous fuzzy match — the happy path the feature enables.
# --------------------------------------------------------------------------- #


def test_unambiguous_fuzzy_match_returns_existing_entity(monkeypatch):
    """WHY: a single high-confidence candidate is exactly when fuzzy matching
    adds value — "memory master" should collapse onto the existing
    "MemoryMaster" entity instead of spawning a duplicate.
    """
    monkeypatch.setenv(_FLAG, "1")
    conn = _fresh_db()
    canonical = resolve_or_create(conn, "MemoryMaster")
    assert canonical > 0
    before = _entity_count(conn)

    matched = resolve_or_create(conn, "memory master")  # norm: memory-master
    assert matched == canonical, "unambiguous near-match should reuse the entity"
    assert _entity_count(conn) == before, "no new entity should be created"


def test_unambiguous_match_records_new_alias_for_fast_path(monkeypatch):
    """WHY: once a fuzzy match is accepted, the surface form should be recorded
    as an alias so the NEXT lookup is an exact hit (cheap) — and so the
    registry's alias graph reflects reality.
    """
    monkeypatch.setenv(_FLAG, "1")
    conn = _fresh_db()
    canonical = resolve_or_create(conn, "MemoryMaster")
    resolve_or_create(conn, "memory master")

    alias_rows = conn.execute(
        "SELECT alias FROM entity_aliases WHERE entity_id = ?", (canonical,)
    ).fetchall()
    aliases = {r[0] for r in alias_rows}
    assert "memorymaster" in aliases
    assert "memory-master" in aliases, "accepted fuzzy form not recorded as alias"


# --------------------------------------------------------------------------- #
# 3. Ambiguous-refuse — the core anti-hallucination guarantee.
# --------------------------------------------------------------------------- #


def test_ambiguous_match_refuses_and_creates_nothing(monkeypatch):
    """WHY: this is the whole reason the resolver is 'guarded'. When a subject
    is roughly equidistant from two existing entities, picking either one is a
    hallucinated merge. The guard must REFUSE: return 0 AND leave the DB
    untouched (no new entity, no stolen alias).

    "tokencaches" sits between "TokenCache" (~0.95) and "TokenCacher" (~0.91) —
    both above the accept threshold and within the ambiguity margin — and
    matches NEITHER exactly, so it genuinely exercises the fuzzy tie path.
    """
    conn = _fresh_db()
    # Seed the two confusable entities with fuzzy OFF so they stay distinct
    # (turning it on first would itself collapse them — which is the bug we're
    # guarding against, just on the setup side).
    monkeypatch.delenv(_FLAG, raising=False)
    id_a = resolve_or_create(conn, "TokenCache")
    id_b = resolve_or_create(conn, "TokenCacher")
    assert id_a != id_b
    before = _entity_count(conn)

    monkeypatch.setenv(_FLAG, "1")
    result = resolve_or_create(conn, "tokencaches")
    assert result == 0, "ambiguous fuzzy match must be REFUSED, not guessed"
    assert _entity_count(conn) == before, (
        "a refused match must not create a polluting near-duplicate entity"
    )
    # And it must NOT have stolen an alias onto either candidate.
    stolen = conn.execute(
        "SELECT COUNT(*) FROM entity_aliases WHERE alias = 'tokencaches'"
    ).fetchone()[0]
    assert stolen == 0


# --------------------------------------------------------------------------- #
# 4. Below-threshold creates new — fuzzy must not over-reach.
# --------------------------------------------------------------------------- #


def test_below_threshold_creates_new_entity(monkeypatch):
    """WHY: a guard that refuses too eagerly is as broken as one that merges
    too eagerly. A subject with no real similarity to anything must still get
    its own entity — the fuzzy path only intercepts CONFIDENT matches.
    """
    monkeypatch.setenv(_FLAG, "1")
    conn = _fresh_db()
    resolve_or_create(conn, "Qdrant")
    resolve_or_create(conn, "SQLite")
    before = _entity_count(conn)

    new_id = resolve_or_create(conn, "xyz123")  # similar to nothing present
    assert new_id > 0, "below-threshold subject should create a new entity"
    assert _entity_count(conn) == before + 1


# --------------------------------------------------------------------------- #
# 5. Entity fanout unaffected when the flag is off (bit-identical guard).
# --------------------------------------------------------------------------- #


def test_fanout_unaffected_by_flag(monkeypatch):
    """WHY: the fuzzy flag lives in the registry, but the recall fanout
    (``_entity_fanout_claim_ids``) resolves entities by EXACT alias only. The
    flag must not change fanout output — recall stays bit-identical whether or
    not an operator has opted into fuzzy *ingest* resolution.
    """
    from memorymaster.recall.context_hook import _entity_fanout_claim_ids

    # Build a DB with an entity + linked claim that the fanout can resolve.
    conn = _fresh_db()
    # claims table here needs the columns the fanout SQL reads.
    conn.execute("DROP TABLE claims")
    conn.execute(
        "CREATE TABLE claims (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "text TEXT, status TEXT DEFAULT 'confirmed', visibility TEXT DEFAULT 'public', "
        "updated_at TEXT DEFAULT '2026-01-01', entity_id INTEGER)"
    )
    eid = resolve_or_create(conn, "context_hook.py")
    conn.execute(
        "INSERT INTO claims (text, entity_id) VALUES ('about context_hook.py', ?)",
        (eid,),
    )
    conn.commit()

    class _Store:
        def connect(self):
            return conn

    store = _Store()
    prompt = "fix context_hook.py now"

    monkeypatch.delenv(_FLAG, raising=False)
    off = _entity_fanout_claim_ids(store, prompt, set())

    monkeypatch.setenv(_FLAG, "1")
    on = _entity_fanout_claim_ids(store, prompt, set())

    assert off == on, "fuzzy flag must not alter exact-match fanout output"
    assert off, "precondition: fanout should resolve the linked claim"


# --------------------------------------------------------------------------- #
# 6. Concurrent safety — SQLite serialization + INSERT OR IGNORE still hold.
# --------------------------------------------------------------------------- #


def test_concurrent_resolve_creates_single_entity(tmp_path, monkeypatch):
    """WHY: enabling fuzzy resolution adds a read (the alias scan) before the
    create. We must prove that read does not open a race window — 10 threads
    resolving the SAME subject must still converge on exactly one entity row,
    relying on SQLite's serialization + INSERT OR IGNORE on canonical_name.
    """
    monkeypatch.setenv(_FLAG, "1")
    db = tmp_path / "concurrent.sqlite"
    # Seed schema once.
    with sqlite3.connect(db) as seed:
        seed.execute(
            "CREATE TABLE claims (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "subject TEXT, entity_id INTEGER)"
        )
        ensure_entity_schema(seed)
        seed.commit()

    barrier = threading.Barrier(10)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            conn = sqlite3.connect(db, timeout=30)
            barrier.wait()  # maximize contention
            resolve_or_create(conn, "MemoryMaster")
            conn.commit()
            conn.close()
        except Exception as exc:  # pragma: no cover - surfaced via assert below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent resolve raised: {errors}"
    with sqlite3.connect(db) as check:
        count = check.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert count == 1, f"expected exactly 1 entity under concurrency, got {count}"
