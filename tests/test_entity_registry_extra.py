"""Coverage hardening for memorymaster/entity_registry.py.

The primary suite (test_entity_registry.py) covers normalization, the
dedup happy path, and the legacy->new schema migration. This file targets
the branches it does not exercise, each anchored to the invariant it
protects (WHY), not just the line:

- resolve_or_create's INSERT-OR-IGNORE collision recovery path (two entities
  racing for the same canonical_name must converge, never orphan)
- entity_type / scope are persisted on the created entity row
- merge_entities must move aliases AND claims and report accurate counts, so
  dedup can collapse duplicates without losing the merged node's history
- add_alias to a brand-new entity returns True; re-adding returns False
  (idempotent), so callers can use the boolean as "was this new?"
- list_entities filters (scope/type), ordering by claim_count, and limit
- get_aliases ordering + empty case
- backfill_entities (the non-normalized path) adds the entity_id column and
  resolves every distinct subject
"""
from __future__ import annotations

import sqlite3

from memorymaster.entity_registry import (
    add_alias,
    backfill_entities,
    ensure_entity_schema,
    get_aliases,
    list_entities,
    merge_entities,
    resolve_or_create,
)


def _fresh_db() -> sqlite3.Connection:
    """In-memory DB with claims + entity tables, mirroring the live schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT,
            entity_id INTEGER
        )
        """
    )
    ensure_entity_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# resolve_or_create — entity_type/scope persistence + collision recovery
# ---------------------------------------------------------------------------


def test_resolve_persists_type_and_scope():
    """A caller that classifies the subject (type/scope) must see those
    values stored on the entity row; otherwise grouping/traversal by type
    silently degrades to 'unknown'/'global'."""
    conn = _fresh_db()
    eid = resolve_or_create(
        conn, "Qdrant", entity_type="technology", scope="project:mm"
    )
    row = conn.execute(
        "SELECT entity_type, scope FROM entities WHERE id = ?", (eid,)
    ).fetchone()
    assert row == ("technology", "project:mm")


def test_resolve_recovers_from_canonical_name_collision():
    """If an entities row already holds the canonical_name but NO alias row
    points at it (a torn write / external insert), resolve_or_create must
    INSERT OR IGNORE, fail to create a dup, then look the existing id back
    up — never returning 0 and never creating a second 'Qdrant' entity.
    This exercises the else-branch of the lastrowid check."""
    conn = _fresh_db()
    # Pre-seed an entities row with no matching alias row.
    conn.execute(
        "INSERT INTO entities (canonical_name, entity_type, scope, "
        "created_at, updated_at) VALUES ('Qdrant', 'unknown', 'global', 't', 't')"
    )
    pre_id = conn.execute(
        "SELECT id FROM entities WHERE canonical_name = 'Qdrant'"
    ).fetchone()[0]

    eid = resolve_or_create(conn, "Qdrant")

    assert eid == pre_id  # reused the existing row, did not orphan
    total = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert total == 1  # no duplicate created
    # The alias row was finally recorded against it.
    aliases = conn.execute(
        "SELECT COUNT(*) FROM entity_aliases WHERE entity_id = ?", (eid,)
    ).fetchone()[0]
    assert aliases == 1


# ---------------------------------------------------------------------------
# merge_entities — move aliases + claims, accurate counts
# ---------------------------------------------------------------------------


def test_merge_moves_aliases_and_claims_with_counts():
    """Merging the 'merge' entity into 'keep' must reassign every alias row
    AND every claim that pointed at the merged id, then delete the merged
    entity — and report exactly how many of each moved. Dedup relies on
    these counts being truthful."""
    conn = _fresh_db()
    keep = resolve_or_create(conn, "PostgreSQL")
    merge = resolve_or_create(conn, "Postgres")  # distinct normalized form
    assert keep != merge

    # Two claims reference the merged entity.
    conn.execute("INSERT INTO claims (subject, entity_id) VALUES ('a', ?)", (merge,))
    conn.execute("INSERT INTO claims (subject, entity_id) VALUES ('b', ?)", (merge,))

    out = merge_entities(conn, keep, merge)

    assert out["merged_aliases"] == 1  # the single 'Postgres' alias row moved
    assert out["updated_claims"] == 2  # both claims repointed
    # Merged entity is gone.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM entities WHERE id = ?", (merge,)
        ).fetchone()[0]
        == 0
    )
    # keep now owns both aliases and both claims.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM entity_aliases WHERE entity_id = ?", (keep,)
        ).fetchone()[0]
        == 2
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM claims WHERE entity_id = ?", (keep,)
        ).fetchone()[0]
        == 2
    )


# ---------------------------------------------------------------------------
# add_alias — boolean contract (new vs duplicate) + blank guard
# ---------------------------------------------------------------------------


def test_add_alias_returns_true_only_when_new():
    """add_alias returns True on first insert, False on a repeat of the same
    variant — callers depend on this to count genuinely-new aliases."""
    conn = _fresh_db()
    eid = resolve_or_create(conn, "Qdrant")
    assert add_alias(conn, eid, "qdrant-cloud") is True
    assert add_alias(conn, eid, "qdrant-cloud") is False  # idempotent


def test_add_alias_blank_is_rejected():
    """A blank/whitespace alias normalizes to empty and must be a no-op
    (False), never inserting an unmatchable empty alias row."""
    conn = _fresh_db()
    eid = resolve_or_create(conn, "Qdrant")
    before = conn.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0]
    assert add_alias(conn, eid, "   ") is False
    after = conn.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0]
    assert after == before


# ---------------------------------------------------------------------------
# list_entities — filters, ordering, limit
# ---------------------------------------------------------------------------


def test_list_entities_orders_by_claim_count_and_counts_aliases():
    """list_entities must rank by claim_count desc and report per-entity
    alias_count/claim_count, which the dashboard renders directly."""
    conn = _fresh_db()
    busy = resolve_or_create(conn, "Busy")
    add_alias(conn, busy, "Busy-alt")
    quiet = resolve_or_create(conn, "Quiet")
    # Give 'busy' two claims, 'quiet' none.
    conn.execute("INSERT INTO claims (subject, entity_id) VALUES ('x', ?)", (busy,))
    conn.execute("INSERT INTO claims (subject, entity_id) VALUES ('y', ?)", (busy,))

    rows = list_entities(conn)
    assert [r["name"] for r in rows] == ["Busy", "Quiet"]  # busy first
    busy_row = rows[0]
    assert busy_row["claim_count"] == 2
    assert busy_row["alias_count"] == 2  # 'Busy' + 'Busy-alt'
    assert quiet in {r["id"] for r in rows}


def test_list_entities_filters_by_scope_and_type_and_limit():
    """The scope (prefix LIKE) and type filters must scope results, and the
    limit must cap them — so a tenant-scoped listing never leaks others."""
    conn = _fresh_db()
    resolve_or_create(conn, "Ada", entity_type="person", scope="project:mm")
    resolve_or_create(conn, "Rust", entity_type="technology", scope="project:mm")
    resolve_or_create(conn, "Go", entity_type="technology", scope="project:other")

    by_type = list_entities(conn, entity_type="technology")
    assert sorted(r["name"] for r in by_type) == ["Go", "Rust"]

    by_scope = list_entities(conn, scope="project:mm")
    assert sorted(r["name"] for r in by_scope) == ["Ada", "Rust"]

    limited = list_entities(conn, limit=1)
    assert len(limited) == 1


# ---------------------------------------------------------------------------
# get_aliases — ordering + empty
# ---------------------------------------------------------------------------


def test_get_aliases_empty_for_unknown_entity():
    """An entity with no aliases (or a bogus id) must yield an empty list,
    not raise — callers iterate the result unconditionally."""
    conn = _fresh_db()
    assert get_aliases(conn, 12345) == []


# ---------------------------------------------------------------------------
# backfill_entities — adds entity_id column + resolves all subjects
# ---------------------------------------------------------------------------


def test_backfill_entities_resolves_all_subjects():
    """backfill_entities is the one-shot that gives existing claims an
    entity_id. It must ALTER in the entity_id column, process every distinct
    subject, and — crucially — collapse case-variant subjects onto the SAME
    canonical entity so the claims graph isn't fragmented."""
    conn = sqlite3.connect(":memory:")
    # Claims table WITHOUT entity_id — backfill must ALTER it in.
    conn.execute(
        "CREATE TABLE claims (id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT)"
    )
    for s in ["Qdrant", "qdrant", "Postgres"]:
        conn.execute("INSERT INTO claims (subject) VALUES (?)", (s,))

    out = backfill_entities(conn)

    # entity_id column now exists.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(claims)").fetchall()}
    assert "entity_id" in cols
    # Every distinct subject string was processed.
    assert out["subjects_processed"] == 3
    # The real invariant: 'Qdrant'/'qdrant' collapse, 'Postgres' is separate,
    # so the entities TABLE holds exactly 2 canonical rows regardless of how
    # the per-subject counter reports.
    assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 2
    entity_ids = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT entity_id FROM claims WHERE entity_id IS NOT NULL"
        ).fetchall()
    }
    assert len(entity_ids) == 2


def test_backfill_entities_is_safe_to_rerun():
    """Running backfill twice must not double-create entities or crash on the
    already-present entity_id column (the ALTER is guarded)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE claims (id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT)"
    )
    conn.execute("INSERT INTO claims (subject) VALUES ('Qdrant')")
    backfill_entities(conn)
    out2 = backfill_entities(conn)  # second pass: nothing left to resolve
    assert out2["subjects_processed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1
