"""Intent-anchored tests for entity community detection.

WHY these tests exist: community detection is only useful if it (a) recovers
the obvious real-life clusters in the entity graph (family vs work vs dev
projects), (b) assigns STABLE community IDs across re-runs so downstream
consumers can reference "community 0" without it shuffling, and (c) stays
strictly optional — no networkx must never crash a released package, and the
env gate must keep entity_stats at zero extra cost by default.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from memorymaster.knowledge.entity_communities import (
    CommunityDetectionUnavailable,
    community_summary,
    compute_communities,
)
from memorymaster.knowledge.entity_graph import EntityGraph

nx = pytest.importorskip("networkx", reason="community detection requires the optional [graph] extra")

NOW = datetime.now(timezone.utc).isoformat()


def _seed_entity(conn, name: str) -> str:
    ent_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO entities (id, name, type, aliases, created_at) VALUES (?, ?, 'person', ?, ?)",
        (ent_id, name, json.dumps([]), NOW),
    )
    return ent_id


def _seed_edge(conn, src: str, tgt: str, weight: float = 1.0) -> None:
    conn.execute(
        "INSERT INTO entity_edges (source_id, target_id, relation, weight, claim_id, created_at)"
        " VALUES (?, ?, 'related_to', ?, 1, ?)",
        (src, tgt, weight, NOW),
    )


@pytest.fixture()
def two_cluster_db(tmp_path):
    """Two obvious real-life clusters joined by ONE weak bridge edge.

    Cluster A (family, 5 nodes, dense): the algorithm must NOT split it.
    Cluster B (dev project, 4 nodes, dense): must NOT be merged into A
    despite the bridge — that is the entire point of modularity clustering.
    """
    db = str(tmp_path / "graph.db")
    eg = EntityGraph(db)
    eg.ensure_tables()
    conn = eg._connect()
    family = {n: _seed_entity(conn, n) for n in ["Mama", "Papa", "Hermano", "Abuela", "Tia"]}
    devs = {n: _seed_entity(conn, n) for n in ["MemoryMaster", "SQLite", "Qdrant", "FastMCP"]}
    fam_ids = list(family.values())
    dev_ids = list(devs.values())
    # Dense intra-cluster edges (clique-ish), heavy weights.
    for i in range(len(fam_ids)):
        for j in range(i + 1, len(fam_ids)):
            _seed_edge(conn, fam_ids[i], fam_ids[j], weight=3.0)
    for i in range(len(dev_ids)):
        for j in range(i + 1, len(dev_ids)):
            _seed_edge(conn, dev_ids[i], dev_ids[j], weight=3.0)
    # One weak bridge: family member mentioned near a dev project once.
    _seed_edge(conn, fam_ids[0], dev_ids[0], weight=0.1)
    conn.commit()
    conn.close()
    return db, family, devs


class TestClusterRecovery:
    def test_two_obvious_clusters_detected_as_two_communities(self, two_cluster_db):
        """The clustering must recover the human-obvious partition, not merge
        across the weak bridge or shatter dense groups into fragments."""
        db, family, devs = two_cluster_db
        communities = compute_communities(db)
        assert len(communities) == 2

        members_by_size = {c["size"] for c in communities}
        assert members_by_size == {5, 4}

        # Each detected community must be PURE: all-family or all-dev.
        top0 = set(communities[0]["top_entities"])
        top1 = set(communities[1]["top_entities"])
        assert top0 <= set(family) or top0 <= set(devs)
        assert top1 <= set(family) or top1 <= set(devs)
        assert (top0 <= set(family)) != (top1 <= set(family))

    def test_biggest_community_gets_id_zero(self, two_cluster_db):
        """Size-rank ordering: community 0 is ALWAYS the biggest — that is the
        contract consumers rely on when they say 'the main cluster'."""
        db, _, _ = two_cluster_db
        communities = compute_communities(db)
        assert communities[0]["community_id"] == 0
        assert communities[0]["size"] == 5
        assert communities[1]["community_id"] == 1
        assert communities[1]["size"] == 4

    def test_top_entities_are_hubs_not_random(self, two_cluster_db):
        """top_entities must be ordered by weighted degree so the label of a
        community reflects its hub, not an arbitrary member."""
        db, family, _ = two_cluster_db
        communities = compute_communities(db)
        fam_community = next(c for c in communities if c["size"] == 5)
        # Mama holds the extra bridge edge -> strictly highest weighted degree.
        assert fam_community["top_entities"][0] == "Mama"


class TestStability:
    def test_rerun_keeps_ids_stable(self, two_cluster_db):
        """Re-running on identical data must produce byte-identical output —
        downstream references to community IDs would silently break otherwise."""
        db, _, _ = two_cluster_db
        first = compute_communities(db)
        second = compute_communities(db)
        assert first == second

    def test_similar_data_keeps_big_community_id(self, two_cluster_db):
        """Adding a small amount of new data must not reshuffle the big
        communities' IDs (deterministic size-rank remap)."""
        db, _, _ = two_cluster_db
        before = compute_communities(db)
        eg = EntityGraph(db)
        conn = eg._connect()
        # Grow the family cluster slightly: still the biggest.
        new_id = _seed_entity(conn, "Primo")
        fam_hub = conn.execute(
            "SELECT id FROM entities WHERE name = 'Mama'"
        ).fetchone()["id"]
        _seed_edge(conn, fam_hub, new_id, weight=3.0)
        conn.commit()
        conn.close()
        after = compute_communities(db)
        assert after[0]["community_id"] == 0
        assert after[0]["size"] == before[0]["size"] + 1
        # Dev cluster keeps its rank-1 slot.
        assert after[1]["size"] == 4


class TestSummaryAndDegradation:
    def test_summary_reports_count_modularity_and_top(self, two_cluster_db):
        db, _, _ = two_cluster_db
        summary = community_summary(db, top_n=5)
        assert summary["count"] == 2
        # Two dense cliques with one weak bridge: strongly modular by design.
        assert summary["modularity"] is not None and summary["modularity"] > 0.3
        assert len(summary["top"]) == 2

    def test_empty_graph_returns_empty_not_crash(self, tmp_path):
        db = str(tmp_path / "empty.db")
        EntityGraph(db).ensure_tables()
        assert compute_communities(db) == []
        assert community_summary(db) == {"count": 0, "modularity": None, "top": []}

    def test_db_without_entity_tables_returns_empty(self, tmp_path):
        import sqlite3

        db = str(tmp_path / "bare.db")
        sqlite3.connect(db).close()
        assert compute_communities(db) == []

    def test_missing_networkx_raises_actionable_error(self, monkeypatch, tmp_path):
        """A released package must NEVER crash on import when the optional dep
        is absent — it must tell the user how to install it."""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "networkx":
                raise ImportError("No module named 'networkx'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(CommunityDetectionUnavailable, match=r"memorymaster\[graph\]"):
            compute_communities(str(tmp_path / "whatever.db"))

    def test_read_only_contract_no_writes_to_db(self, two_cluster_db):
        """compute_communities must be safe against a live brain opened ro:
        the DB file content must be byte-identical after the call."""
        from pathlib import Path

        db, _, _ = two_cluster_db
        before = Path(db).read_bytes()
        compute_communities(db)
        community_summary(db)
        assert Path(db).read_bytes() == before

    def test_store_object_with_db_path_is_accepted(self, two_cluster_db):
        """The public signature is compute_communities(store) — a store-like
        object exposing .db_path must work, not just raw strings."""
        db, _, _ = two_cluster_db

        class FakeStore:
            db_path = db

        assert compute_communities(FakeStore()) == compute_communities(db)


class TestEnvGating:
    def test_entity_stats_cli_off_by_default(self, tmp_path, monkeypatch, capsys):
        """Default OFF: entity-stats must not pay for (or emit) communities."""
        import argparse

        from memorymaster.surfaces.cli_handlers_curation import _handle_entity_stats

        monkeypatch.delenv("MEMORYMASTER_ENTITY_COMMUNITIES", raising=False)
        db = str(tmp_path / "gate.db")
        EntityGraph(db).ensure_tables()
        args = argparse.Namespace(json_output=True)
        assert _handle_entity_stats(args, None, None, db) == 0
        payload = json.loads(capsys.readouterr().out)
        data = payload.get("data", payload)
        assert "communities" not in data

    def test_entity_stats_cli_on_when_env_set(self, two_cluster_db, monkeypatch, capsys):
        import argparse

        from memorymaster.surfaces.cli_handlers_curation import _handle_entity_stats

        monkeypatch.setenv("MEMORYMASTER_ENTITY_COMMUNITIES", "1")
        db, _, _ = two_cluster_db
        args = argparse.Namespace(json_output=True)
        assert _handle_entity_stats(args, None, None, db) == 0
        payload = json.loads(capsys.readouterr().out)
        data = payload.get("data", payload)
        assert data["communities"]["count"] == 2
