"""Tests for v3.9.0 F8 — structural claim_edges schema + walker."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.claim_edges import (
    MENTION_KIND,
    SUPERSEDES_KIND,
    ensure_claim_edges_schema,
    extract_edges_for_claim,
    rebuild_edges,
    walk_neighbors,
)


def _make_db(tmp_path: Path) -> Path:
    """Build a tiny claims table with 5 rows for the edge tests."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(
            """
            CREATE TABLE claims (
                id INTEGER PRIMARY KEY,
                text TEXT,
                human_id TEXT,
                replaced_by_claim_id INTEGER
            );
            INSERT INTO claims (id, text, human_id, replaced_by_claim_id) VALUES
              (1, 'See claim 2 for details and mm-abcd for context.', 'mm-1111', NULL),
              (2, 'Refers back to claim 1.', 'mm-abcd', 5),
              (3, 'No references at all.', 'mm-3333', NULL),
              (4, 'Mentions claim 2 only.', 'mm-4444', NULL),
              (5, 'Replaced claim 2.', 'mm-5555', NULL);
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db


def test_ensure_schema_idempotent(tmp_path):
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        ensure_claim_edges_schema(conn)
        ensure_claim_edges_schema(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "claim_edges" in tables
    finally:
        conn.close()


def test_extract_edges_for_numeric_ref(tmp_path):
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        ensure_claim_edges_schema(conn)
        edges = extract_edges_for_claim(conn, 1, "See claim 2 for details.")
        assert (1, 2, MENTION_KIND) in edges
    finally:
        conn.close()


def test_extract_edges_for_human_id_ref(tmp_path):
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        ensure_claim_edges_schema(conn)
        edges = extract_edges_for_claim(conn, 1, "context from mm-abcd applies.")
        # mm-abcd belongs to claim id 2
        assert (1, 2, MENTION_KIND) in edges
    finally:
        conn.close()


def test_extract_edges_drops_self_reference(tmp_path):
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        ensure_claim_edges_schema(conn)
        # claim 1 mentions itself via mm-1111 — should be dropped
        edges = extract_edges_for_claim(conn, 1, "Note from mm-1111 about claim 1.")
        assert all(dst != 1 for _, dst, _ in edges)
    finally:
        conn.close()


def test_extract_edges_drops_dangling_refs(tmp_path):
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        ensure_claim_edges_schema(conn)
        # claim 99999 doesn't exist — should NOT produce an edge
        edges = extract_edges_for_claim(conn, 1, "See claim 99999.")
        assert all(dst != 99999 for _, dst, _ in edges)
    finally:
        conn.close()


def test_rebuild_edges_writes_to_table(tmp_path):
    db = _make_db(tmp_path)
    counters = rebuild_edges(db)
    assert counters["claims_scanned"] == 5
    assert counters["edges_written"] >= 1
    # supersession: claim 2 has replaced_by_claim_id=5
    assert counters["supersession_edges"] == 1


def test_rebuild_edges_idempotent(tmp_path):
    db = _make_db(tmp_path)
    first = rebuild_edges(db)
    second = rebuild_edges(db)
    # 2nd run should write 0 new edges (PK conflict ignored)
    assert second["edges_written"] == 0
    # Same 5 claims scanned both times
    assert second["claims_scanned"] == first["claims_scanned"]


def test_walk_neighbors_one_hop(tmp_path):
    db = _make_db(tmp_path)
    rebuild_edges(db)
    # claim 1 → claim 2 (mention)
    distances = walk_neighbors(db, [1], max_hops=1, direction="out")
    assert 2 in distances
    assert distances[2] == 1


def test_walk_neighbors_two_hops_chain(tmp_path):
    db = _make_db(tmp_path)
    rebuild_edges(db)
    # 1 → 2 (mention) → 5 (supersession). With max_hops=2 from seed 1, both 2 and 5 reached.
    distances = walk_neighbors(db, [1], max_hops=2, direction="out")
    assert 2 in distances
    assert 5 in distances
    assert distances[5] == 2


def test_walk_neighbors_seeds_excluded_from_result(tmp_path):
    db = _make_db(tmp_path)
    rebuild_edges(db)
    distances = walk_neighbors(db, [1, 2], max_hops=2)
    assert 1 not in distances
    assert 2 not in distances


def test_walk_neighbors_handles_missing_table(tmp_path):
    """Defensive: if claim_edges doesn't exist yet, walker returns {} not crash."""
    db = tmp_path / "no-edges.db"
    sqlite3.connect(str(db)).close()  # empty db
    assert walk_neighbors(db, [1, 2, 3], max_hops=2) == {}


def test_walk_neighbors_empty_seeds_returns_empty(tmp_path):
    db = _make_db(tmp_path)
    rebuild_edges(db)
    assert walk_neighbors(db, [], max_hops=2) == {}


def test_walk_neighbors_zero_max_hops_returns_empty(tmp_path):
    db = _make_db(tmp_path)
    rebuild_edges(db)
    assert walk_neighbors(db, [1], max_hops=0) == {}
