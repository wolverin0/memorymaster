"""Red contract for composing the canonical entity registry and graph schema."""

from __future__ import annotations

import sqlite3

from memorymaster.core.service import MemoryService
from memorymaster.knowledge.entity_graph import EntityGraph


def test_normal_init_produces_a_graph_ready_entity_schema(tmp_path):
    db_path = tmp_path / "entity-composition.db"
    MemoryService(db_path, workspace_root=tmp_path).init_db()

    graph = EntityGraph(str(db_path))
    graph.assert_ready()

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        entity_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(entities)")
        }

    assert {"entities", "entity_aliases", "entity_edges", "claim_entity_links"} <= tables
    assert {"id", "canonical_name", "entity_type", "scope"} <= entity_columns
    assert graph.get_stats() == {
        "entities": 0,
        "edges": 0,
        "claim_links": 0,
        "by_type": {},
    }
