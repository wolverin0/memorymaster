"""Regression: merge_entities must be atomic and survive alias collisions.

WHY THIS MATTERS: merge_entities moves all of merge_id's alias rows onto
keep_id with a bulk UPDATE. The entity_aliases UNIQUE(entity_id, variant_key)
constraint means that if keep_id already owns an alias with the same
variant_key as one on merge_id, a plain UPDATE raises sqlite3.IntegrityError
*partway through* — aliases/claims half-moved, the merged entity NOT deleted.
That leaves a corrupted entity graph (orphan aliases, two live entities that
were supposed to be one). The merge must instead skip colliding rows, clean
them up, and roll back entirely on any failure. These tests anchor on that
INVARIANT (graph stays consistent), not on the specific SQL used.
"""
from __future__ import annotations

import sqlite3

import pytest

from memorymaster.entity_registry import (
    add_alias,
    ensure_entity_schema,
    merge_entities,
    resolve_or_create,
)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys=ON")
    ensure_entity_schema(c)
    c.execute(
        "CREATE TABLE claims (id INTEGER PRIMARY KEY, subject TEXT, entity_id INTEGER)"
    )
    yield c
    c.close()


def test_merge_with_colliding_variant_key_does_not_corrupt_graph(conn):
    keep = resolve_or_create(conn, "Qdrant")
    merge = resolve_or_create(conn, "QdrantDB")
    # Force a shared variant_key on BOTH entities — this is what would trip
    # the UNIQUE(entity_id, variant_key) constraint on a naive bulk UPDATE.
    add_alias(conn, keep, "shared-alias")
    add_alias(conn, merge, "shared-alias")
    conn.execute(
        "INSERT INTO claims (subject, entity_id) VALUES ('QdrantDB', ?)", (merge,)
    )
    conn.commit()

    out = merge_entities(conn, keep, merge)

    # The merged entity is gone — not stranded by a mid-merge IntegrityError.
    assert conn.execute(
        "SELECT COUNT(*) FROM entities WHERE id = ?", (merge,)
    ).fetchone()[0] == 0
    # No alias rows still point at the dead entity.
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_aliases WHERE entity_id = ?", (merge,)
    ).fetchone()[0] == 0
    # Claims were reassigned to the surviving entity.
    assert conn.execute(
        "SELECT COUNT(*) FROM claims WHERE entity_id = ?", (keep,)
    ).fetchone()[0] == 1
    assert out["updated_claims"] == 1


def test_merge_rolls_back_on_failure(conn):
    """If the merge can't complete, NOTHING is committed — no half-merged graph.

    We simulate a failure by dropping the claims table mid-flight via a
    sabotaged connection so the UPDATE claims step raises. The entity and its
    aliases must remain exactly as they were (rolled back), not partially moved.
    """
    keep = resolve_or_create(conn, "Keep")
    merge = resolve_or_create(conn, "Merge")
    conn.commit()

    aliases_before = conn.execute(
        "SELECT entity_id, variant_key FROM entity_aliases ORDER BY id"
    ).fetchall()

    # Drop the claims table so the UPDATE claims inside merge raises.
    conn.execute("DROP TABLE claims")

    with pytest.raises(sqlite3.OperationalError):
        merge_entities(conn, keep, merge)

    # Both entities still exist and aliases are unchanged — clean rollback.
    assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 2
    aliases_after = conn.execute(
        "SELECT entity_id, variant_key FROM entity_aliases ORDER BY id"
    ).fetchall()
    assert aliases_after == aliases_before
