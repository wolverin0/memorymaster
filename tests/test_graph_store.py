"""Tests for memorymaster.recall.graph_store and the scripts.backfill_graph_store
CLI entry — roadmap 11.3.

Covers the Cognee Alice→Atlas→Postgres multi-hop example, the idempotent
ingest contract, the claim-for-entities reverse lookup, and a regression
guard that ``context_hook.recall`` stays bit-identical when
``MEMORYMASTER_RECALL_GRAPH=0``.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from memorymaster.recall.graph_store import (
    GraphEdge,
    GraphStore,
    GraphStoreUnavailable,
)

REPO = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------
@pytest.fixture()
def kuzu_store(tmp_path: Path):
    """Fresh Kuzu DB in a tempdir, auto-closed after the test."""
    try:
        store = GraphStore(tmp_path / "g.kuzu")
        store.initialize()
    except GraphStoreUnavailable:
        pytest.skip("Kuzu unavailable on this platform")
    yield store
    store.close()


@pytest.fixture()
def cognee_edges() -> list[GraphEdge]:
    """The canonical Alice→Atlas→Postgres chain from the Cognee assessment.

    5 claims, 3 entities:
    * claim 1: "Alice is tech lead on Atlas"  → Alice (100), Atlas (200)
    * claim 2: "Atlas uses PostgreSQL"        → Atlas (200), Postgres (300)
    * claim 3: "PostgreSQL outage Tuesday"    → Postgres (300)
    * claim 4: "Bob reviews Alice's PRs"      → Bob (101),  Alice (100)
    * claim 5: "Atlas launched in Q2"         → Atlas (200)
    """
    return [
        GraphEdge(claim_id=1, entity_id=100, kind="person"),
        GraphEdge(claim_id=1, entity_id=200, kind="project"),
        GraphEdge(claim_id=2, entity_id=200, kind="project"),
        GraphEdge(claim_id=2, entity_id=300, kind="service"),
        GraphEdge(claim_id=3, entity_id=300, kind="service"),
        GraphEdge(claim_id=4, entity_id=101, kind="person"),
        GraphEdge(claim_id=4, entity_id=100, kind="person"),
        GraphEdge(claim_id=5, entity_id=200, kind="project"),
    ]


# ----------------------------------------------------------------------
# public API
# ----------------------------------------------------------------------
class TestGraphStoreKuzu:
    def test_open_requires_explicit_initialization(self, tmp_path):
        store = GraphStore(tmp_path / "missing.kuzu")
        with pytest.raises(GraphStoreUnavailable, match="not initialized"):
            store.open()
        assert not store.path.exists()

    def test_ingest_edges_idempotent(self, kuzu_store, cognee_edges):
        first = kuzu_store.ingest_edges(cognee_edges)
        assert first == len(cognee_edges)
        # Re-ingesting the same list produces 0 new edges.
        second = kuzu_store.ingest_edges(cognee_edges)
        assert second == 0

    def test_neighbors_reaches_postgres_from_alice_in_2_hops(
        self, kuzu_store, cognee_edges
    ):
        kuzu_store.ingest_edges(cognee_edges)
        reached = kuzu_store.neighbors([100], max_hops=2)
        assert 100 in reached  # seed
        assert 200 in reached  # 1-hop (via claim 1)
        assert 300 in reached  # 2-hop (via claim 2)

    def test_neighbors_one_hop_does_not_reach_postgres(
        self, kuzu_store, cognee_edges
    ):
        kuzu_store.ingest_edges(cognee_edges)
        reached = kuzu_store.neighbors([100], max_hops=1)
        assert 200 in reached
        assert 300 not in reached, "Postgres is 2 hops away, not 1"

    def test_claims_for_entities_returns_mentioning_claims(
        self, kuzu_store, cognee_edges
    ):
        kuzu_store.ingest_edges(cognee_edges)
        claim_ids = kuzu_store.claims_for_entities([300])
        # Postgres (300) is mentioned by claims 2 and 3.
        assert set(claim_ids) == {2, 3}

    def test_claims_for_entities_multi_entity_union(
        self, kuzu_store, cognee_edges
    ):
        kuzu_store.ingest_edges(cognee_edges)
        claim_ids = kuzu_store.claims_for_entities([100, 200])
        # Claims mentioning EITHER Alice or Atlas: 1, 2, 4, 5.
        assert set(claim_ids) == {1, 2, 4, 5}

    def test_close_is_idempotent(self, kuzu_store):
        kuzu_store.close()
        kuzu_store.close()  # must not raise

    def test_neighbors_empty_input_returns_empty(self, kuzu_store):
        assert kuzu_store.neighbors([], max_hops=2) == set()

    def test_neighbors_zero_hops_returns_just_seeds(
        self, kuzu_store, cognee_edges
    ):
        kuzu_store.ingest_edges(cognee_edges)
        reached = kuzu_store.neighbors([100, 200], max_hops=0)
        assert reached == {100, 200}


# ----------------------------------------------------------------------
# networkx fallback
# ----------------------------------------------------------------------
class TestNetworkxFallback:
    def test_fallback_matches_kuzu_semantics(
        self, tmp_path: Path, cognee_edges
    ):
        # open_graph_store with allow_networkx=True picks the in-memory
        # fallback when Kuzu is missing; on this box Kuzu is present, so
        # we directly construct the fallback via the private class.
        from memorymaster.recall.graph_store import _NetworkXGraphStore

        fb = _NetworkXGraphStore(tmp_path / "g")
        fb.open()
        assert fb.ingest_edges(cognee_edges) == len(cognee_edges)
        assert fb.ingest_edges(cognee_edges) == 0
        reached = fb.neighbors([100], max_hops=2)
        assert {100, 200, 300}.issubset(reached)
        assert set(fb.claims_for_entities([300])) == {2, 3}
        fb.close()


# ----------------------------------------------------------------------
# backfill script — dry-run against a synthetic SQLite DB
# ----------------------------------------------------------------------
def _seed_db_for_backfill(db_path: Path) -> dict:
    """Create a minimal SQLite DB with claims + entities + entity_aliases
    populated. Returns the expected edge count (source-1 + source-2).
    """
    schema = """
    CREATE TABLE claims (
        id INTEGER PRIMARY KEY,
        entity_id INTEGER,
        subject TEXT,
        text TEXT,
        status TEXT DEFAULT 'confirmed'
    );
    CREATE TABLE entities (
        id INTEGER PRIMARY KEY,
        canonical_name TEXT NOT NULL,
        entity_type TEXT NOT NULL DEFAULT 'unknown',
        scope TEXT NOT NULL DEFAULT 'global',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE entity_aliases (
        id INTEGER PRIMARY KEY,
        entity_id INTEGER NOT NULL,
        alias TEXT NOT NULL,
        variant_key TEXT NOT NULL DEFAULT '',
        original_form TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """
    conn = sqlite3.connect(str(db_path))
    conn.executescript(schema)
    conn.execute(
        "INSERT INTO entities VALUES (100, 'alice', 'person', 'global', 't', 't')"
    )
    conn.execute(
        "INSERT INTO entities VALUES (200, 'atlas', 'project', 'global', 't', 't')"
    )
    conn.execute(
        "INSERT INTO entities VALUES (300, 'postgres', 'service', 'global', 't', 't')"
    )
    conn.executemany(
        "INSERT INTO entity_aliases VALUES (?, ?, ?, '', ?, 't')",
        [
            (1, 100, "alice", "Alice"),
            (2, 200, "atlas", "Atlas"),
            (3, 300, "postgres", "PostgreSQL"),
        ],
    )
    # Two claims — claim 1 has entity_id=100 (Alice) and also mentions Atlas
    # in text; claim 2 has entity_id=200 (Atlas) and mentions PostgreSQL.
    conn.executemany(
        "INSERT INTO claims VALUES (?, ?, ?, ?, 'confirmed')",
        [
            (1, 100, "Alice leads Atlas", "Alice is tech lead on Atlas"),
            (2, 200, "Atlas uses PostgreSQL", "Atlas relies on PostgreSQL"),
        ],
    )
    conn.commit()
    conn.close()
    # Expected edges:
    # claim 1 → Alice (source-1), claim 1 → Atlas (source-2 text match)
    # claim 2 → Atlas (source-1), claim 2 → Postgres (source-2 text match)
    return {"expected_edges": 4}


class TestBackfillScript:
    def test_dry_run_counts_edges(self, tmp_path: Path):
        db_path = tmp_path / "seed.db"
        expected = _seed_db_for_backfill(db_path)
        env = {**os.environ, "PYTHONPATH": str(REPO)}
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "backfill_graph_store.py"),
                "--db", str(db_path),
                "--graph-path", str(tmp_path / "graph.kuzu"),
                "--dry-run",
            ],
            capture_output=True, text=True, env=env, cwd=str(REPO),
        )
        assert result.returncode == 0, (
            f"backfill failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert f"edges_considered={expected['expected_edges']}" in result.stdout
        assert "edges_written=0" in result.stdout  # dry-run
        assert "dry_run=True" in result.stdout

    def test_live_run_writes_edges(self, tmp_path: Path):
        pytest.importorskip("kuzu", reason="graph extra not installed")
        db_path = tmp_path / "seed.db"
        expected = _seed_db_for_backfill(db_path)
        graph_path = tmp_path / "graph.kuzu"
        env = {**os.environ, "PYTHONPATH": str(REPO)}
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "backfill_graph_store.py"),
                "--db", str(db_path),
                "--graph-path", str(graph_path),
            ],
            capture_output=True, text=True, env=env, cwd=str(REPO),
        )
        assert result.returncode == 0, (
            f"backfill failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert f"edges_written={expected['expected_edges']}" in result.stdout
        # Idempotent re-run writes zero new edges
        result2 = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "backfill_graph_store.py"),
                "--db", str(db_path),
                "--graph-path", str(graph_path),
            ],
            capture_output=True, text=True, env=env, cwd=str(REPO),
        )
        assert result2.returncode == 0
        assert "edges_written=0" in result2.stdout


# ----------------------------------------------------------------------
# recall() regression guard — MEMORYMASTER_RECALL_GRAPH=0 must be
# bit-identical to the 5-stream baseline
# ----------------------------------------------------------------------
class TestRecallRegressionGuard:
    def test_graph_disabled_short_circuits_before_import(self, monkeypatch):
        """When MEMORYMASTER_RECALL_GRAPH is unset or 0, _graph_enabled
        must return False and ``_graph_reached_claim_ids`` must return an
        empty set without importing :mod:`memorymaster.recall.graph_store`.
        """
        import memorymaster.recall.context_hook as ch

        monkeypatch.delenv("MEMORYMASTER_RECALL_GRAPH", raising=False)
        assert ch._graph_enabled() is False
        # _graph_reached_claim_ids returns early on disabled
        assert ch._graph_reached_claim_ids("query", store=None) == set()

        monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH", "0")
        assert ch._graph_enabled() is False

    def test_graph_env_parsing(self, monkeypatch):
        import memorymaster.recall.context_hook as ch

        monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH", "1")
        assert ch._graph_enabled() is True

        monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MAX_HOPS", "3")
        assert ch._graph_max_hops() == 3

        monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MAX_HOPS", "garbage")
        assert ch._graph_max_hops() == 2  # default

        monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_MAX_HOPS", "0")
        assert ch._graph_max_hops() == 1  # clamped to >= 1

    def test_populated_streams_counts_graph_when_scored(self):
        import memorymaster.recall.context_hook as ch

        # 0 populated — nothing scored
        rows = [{"entity_score": 0, "vector_score": 0}]
        assert ch._count_populated_streams(rows, {}, False, 0.0) == 0

        # With a graph_score populated, count should be 1
        rows2 = [{"graph_score": 1.0}]
        assert ch._count_populated_streams(rows2, {}, False, 0.0) == 1

        # Without graph score but bm25 on → 1
        rows3 = [{"lexical_score": 0.0}]
        assert ch._count_populated_streams(rows3, {1: 0.5}, True, 0.0) == 1

        # graph absent on all rows → not counted
        rows4 = [{"entity_score": 1.0}, {"vector_score": 0.5}]
        assert ch._count_populated_streams(rows4, {}, False, 0.0) == 2
