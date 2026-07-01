"""Hebbian potentiation + Ebbinghaus decay on entity-graph edges.

WHY this matters (MemPalace forgetting curve): an entity graph that only ever
ADDS edges treats a relationship asserted once five years ago the same as one
reinforced every day this week. That makes recall stale-biased and unbounded.
This feature makes the graph behave like memory — co-occurrence STRENGTHENS an
edge (Hebbian "fire together, wire together") and elapsed time DECAYS it
(Ebbinghaus), with a floor so a path is dimmed but never fully erased.

Each test below anchors on the *requirement*, not the implementation: it would
FAIL if the underlying lifecycle (potentiate-on-use, decay-on-time, floor,
weight-ordered recall, default-OFF safety) regressed — even if the code still
"runs".
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.govern.jobs import decay
from memorymaster.knowledge.entity_graph import EntityGraph

HEBBIAN_FLAG = "MEMORYMASTER_HEBBIAN_DECAY"


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "hebbian.db"


@pytest.fixture
def graph(db_path):
    g = EntityGraph(str(db_path))
    g.ensure_tables()
    return g


@pytest.fixture
def enable_decay(monkeypatch):
    """Turn the recall-altering feature ON for tests that exercise decay."""
    monkeypatch.setenv(HEBBIAN_FLAG, "1")


def _seed_entity(graph: EntityGraph, name: str) -> str:
    conn = graph._connect()
    try:
        ent_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO entities (id, name, type, aliases, created_at) VALUES (?, ?, 'concept', '[]', ?)",
            (ent_id, name, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return ent_id
    finally:
        conn.close()


def _read_edge(graph: EntityGraph, src: str, tgt: str, relation: str):
    conn = graph._connect()
    try:
        return conn.execute(
            "SELECT weight, last_reinforced_at FROM entity_edges "
            "WHERE source_id = ? AND target_id = ? AND relation = ?",
            (src, tgt, relation),
        ).fetchone()
    finally:
        conn.close()


def _insert_edge(graph: EntityGraph, src, tgt, relation, weight, last_reinforced_at, claim_id):
    conn = graph._connect()
    try:
        conn.execute(
            "INSERT INTO entity_edges "
            "(source_id, target_id, relation, weight, claim_id, created_at, last_reinforced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (src, tgt, relation, weight, claim_id,
             datetime.now(timezone.utc).isoformat(), last_reinforced_at),
        )
        conn.commit()
    finally:
        conn.close()


def test_edge_potentiation_increments_weight(graph):
    """Reinforcing the SAME relation must strengthen it (Hebbian) and re-stamp
    last_reinforced_at — otherwise repeated co-occurrence carries no signal and
    the decay clock can never be reset by usage."""
    src = _seed_entity(graph, "Alice")
    tgt = _seed_entity(graph, "ProjectX")
    conn = graph._connect()
    try:
        graph._upsert_edge(conn, src, tgt, "works_on", claim_id=1)
        conn.commit()
        first = conn.execute(
            "SELECT weight, last_reinforced_at FROM entity_edges "
            "WHERE source_id=? AND target_id=? AND relation=?",
            (src, tgt, "works_on"),
        ).fetchone()
        assert first["weight"] == pytest.approx(1.0)
        assert first["last_reinforced_at"]  # stamped on first insert too

        graph._upsert_edge(conn, src, tgt, "works_on", claim_id=2)
        conn.commit()
        second = conn.execute(
            "SELECT weight, last_reinforced_at FROM entity_edges "
            "WHERE source_id=? AND target_id=? AND relation=?",
            (src, tgt, "works_on"),
        ).fetchone()
    finally:
        conn.close()

    assert second["weight"] > 1.0, "second co-occurrence must potentiate the edge"
    assert second["last_reinforced_at"] >= first["last_reinforced_at"], (
        "reinforcement must refresh the Hebbian timestamp the decay job reads"
    )


def test_edge_decay_reduces_weight(graph, enable_decay):
    """An edge untouched for 30 days must lose weight (Ebbinghaus). If it didn't,
    the graph would never forget and old relationships would dominate recall
    forever — the exact failure this feature exists to prevent."""
    src = _seed_entity(graph, "OldFact")
    tgt = _seed_entity(graph, "Server")
    thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    _insert_edge(graph, src, tgt, "related_to", weight=2.0,
                 last_reinforced_at=thirty_days_ago, claim_id=10)

    result = decay.decay_entity_edges(graph)
    assert result["enabled"] is True
    assert result["decayed"] >= 1

    row = _read_edge(graph, src, tgt, "related_to")
    assert row["weight"] < 2.0, "30-day-old edge must decay below its starting weight"
    assert row["weight"] >= decay.EDGE_WEIGHT_FLOOR, "decay must never drop below the floor"


def test_edge_decay_floor(graph, enable_decay):
    """A nearly-dead edge decayed over an extreme span must clamp at the floor,
    never reaching 0. The floor is the 'trace' of a faded memory — a fully-zeroed
    edge would silently sever a recall path instead of merely dimming it."""
    src = _seed_entity(graph, "AncientA")
    tgt = _seed_entity(graph, "AncientB")
    long_ago = (datetime.now(timezone.utc) - timedelta(days=1000)).isoformat()
    _insert_edge(graph, src, tgt, "related_to", weight=0.02,
                 last_reinforced_at=long_ago, claim_id=11)

    decay.decay_entity_edges(graph)

    row = _read_edge(graph, src, tgt, "related_to")
    assert row["weight"] == pytest.approx(decay.EDGE_WEIGHT_FLOOR), (
        "weight must clamp exactly at the floor, not undershoot toward zero"
    )


def test_find_related_claims_respects_weight(graph):
    """Two competing paths from the same seed must surface the STRONGER edge's
    claim first. Recall ordering is the whole point of weighting — if a heavily
    reinforced relationship didn't outrank a barely-there one, potentiation and
    decay would be invisible to the caller and pointless."""
    seed = _seed_entity(graph, "Hub")
    strong_tgt = _seed_entity(graph, "StrongNeighbor")
    weak_tgt = _seed_entity(graph, "WeakNeighbor")
    _insert_edge(graph, seed, strong_tgt, "related_to", weight=5.0,
                 last_reinforced_at=datetime.now(timezone.utc).isoformat(), claim_id=100)
    _insert_edge(graph, seed, weak_tgt, "related_to", weight=0.1,
                 last_reinforced_at=datetime.now(timezone.utc).isoformat(), claim_id=200)

    conn = graph._connect()
    try:
        conn.execute("INSERT INTO claim_entity_links (claim_id, entity_id) VALUES (?, ?)", (100, strong_tgt))
        conn.execute("INSERT INTO claim_entity_links (claim_id, entity_id) VALUES (?, ?)", (200, weak_tgt))
        conn.commit()
    finally:
        conn.close()

    result = graph.find_related_claims(["Hub"], hops=1, limit=10)
    assert 100 in result and 200 in result, "both reachable claims must appear"
    assert result.index(100) < result.index(200), (
        "high-weight edge's claim must outrank the low-weight edge's claim"
    )


def test_decay_is_noop_on_missing_column(db_path, enable_decay):
    """A DB created before this feature lacks last_reinforced_at. Decay must NOT
    raise on it — a release upgrade can't crash the steward cycle just because an
    old graph hasn't been migrated yet. Either graceful-skip or auto-migrate is
    acceptable; a traceback is not."""
    conn_path = str(db_path)
    import sqlite3
    conn = sqlite3.connect(conn_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE entity_edges (
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            relation TEXT NOT NULL DEFAULT 'related_to',
            weight REAL NOT NULL DEFAULT 1.0,
            claim_id INTEGER,
            created_at TEXT NOT NULL,
            PRIMARY KEY (source_id, target_id, relation)
        );
        INSERT INTO entity_edges (source_id, target_id, relation, weight, claim_id, created_at)
        VALUES ('a', 'b', 'related_to', 1.5, 1, '2020-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    # Minimal store shim exposing .connect()/.db_path like the real SQLite store.
    class _Store:
        def __init__(self, path):
            self.db_path = path

        def connect(self):
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            return c

    # Must not raise.
    result = decay.decay_entity_edges(_Store(conn_path))
    assert result["enabled"] is True
    assert result["decayed"] == 0
    assert "skipped" in result, "missing column should be skipped, not crash"


def test_default_off_is_byte_identical(graph, monkeypatch):
    """RECALL-ALTERING guarantee: with MEMORYMASTER_HEBBIAN_DECAY unset, the
    decay job must mutate NOTHING. This proves the default path is unchanged from
    the pre-feature baseline — the contract for shipping a recall-altering change
    into a released package."""
    monkeypatch.delenv(HEBBIAN_FLAG, raising=False)
    src = _seed_entity(graph, "Untouched")
    tgt = _seed_entity(graph, "AlsoUntouched")
    old = (datetime.now(timezone.utc) - timedelta(days=500)).isoformat()
    _insert_edge(graph, src, tgt, "related_to", weight=3.0,
                 last_reinforced_at=old, claim_id=99)

    result = decay.decay_entity_edges(graph)
    assert result == {"enabled": False, "decayed": 0}

    row = _read_edge(graph, src, tgt, "related_to")
    assert row["weight"] == pytest.approx(3.0), "default-OFF path must not touch any weight"


def test_run_cycle_includes_edge_decay(monkeypatch, tmp_path):
    """run_cycle must surface the edge-decay phase in its result dict so the
    steward and dashboard can observe it — and must remain failure-isolated
    (no exception escapes). This anchors the integration contract, not just the
    unit behavior."""
    from memorymaster.core.service import MemoryService

    monkeypatch.setenv(HEBBIAN_FLAG, "1")
    import sqlite3

    db_file = str(tmp_path / "cycle.db")
    svc = MemoryService(db_file, workspace_root=str(tmp_path))
    svc.init_db()

    # Seed a real entity_edges row (the only table this feature mutates) directly
    # on the service DB. We create the table with raw DDL rather than
    # EntityGraph.ensure_tables() because the graph's executescript also
    # (re)declares an `entities` table that collides with the registry `entities`
    # init_db() already created — a pre-existing schema-name overlap unrelated to
    # this feature. The decay job reads ONLY entity_edges, so this is sufficient.
    conn = sqlite3.connect(db_file)
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS entity_edges (
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            relation TEXT NOT NULL DEFAULT 'related_to',
            weight REAL NOT NULL DEFAULT 1.0,
            claim_id INTEGER,
            created_at TEXT NOT NULL,
            last_reinforced_at TEXT,
            PRIMARY KEY (source_id, target_id, relation)
        );
        """
    )
    conn.execute(
        "INSERT INTO entity_edges "
        "(source_id, target_id, relation, weight, claim_id, created_at, last_reinforced_at) "
        "VALUES ('cycle-a', 'cycle-b', 'related_to', 2.0, 1, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), old),
    )
    conn.commit()
    conn.close()

    result = svc.run_cycle()

    assert "entity_edge_decay" in result, "cycle must report the edge-decay phase"
    phase = result["entity_edge_decay"]
    assert "error" not in phase, f"edge decay phase must not error: {phase}"
    assert phase.get("enabled") is True
    assert phase.get("decayed", 0) >= 1, "the 20-day-old seeded edge should decay in-cycle"
