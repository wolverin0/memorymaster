"""P2-D contracts for one versioned entity registry and graph schema."""

from __future__ import annotations

import sqlite3
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.knowledge.entity_graph import EntityGraph, EntityGraphNotReady
from memorymaster.surfaces import mcp_server


def _table_columns(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _create_legacy_graph_first(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE entities (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
                aliases TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
            );
            CREATE TABLE entity_edges (
                source_id TEXT NOT NULL, target_id TEXT NOT NULL,
                relation TEXT NOT NULL, weight REAL NOT NULL DEFAULT 1.0,
                claim_id INTEGER, created_at TEXT NOT NULL,
                PRIMARY KEY (source_id, target_id, relation)
            );
            CREATE TABLE claim_entity_links (
                claim_id INTEGER NOT NULL, entity_id TEXT NOT NULL,
                PRIMARY KEY (claim_id, entity_id)
            );
            INSERT INTO entities VALUES
                ('legacy-alice', 'Alice', 'person', '["A. Example"]', '2026-01-01T00:00:00Z');
            """
        )


def test_graph_first_database_migrates_to_canonical_registry(tmp_path: Path) -> None:
    db_path = tmp_path / "graph-first.db"
    _create_legacy_graph_first(db_path)

    MemoryService(db_path, workspace_root=tmp_path).init_db()
    EntityGraph(str(db_path)).assert_ready()

    assert {"id", "canonical_name", "entity_type", "scope"} <= _table_columns(
        db_path, "entities"
    )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT canonical_name FROM entities WHERE canonical_name = 'Alice'"
        ).fetchone() == ("Alice",)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_registry_first_database_adds_graph_without_replacing_entities(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "registry-first.db"
    migration = import_module(
        "memorymaster.stores.migrations.0013_canonical_entity_graph"
    )
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE claims (id INTEGER PRIMARY KEY);
            CREATE TABLE entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_name TEXT NOT NULL UNIQUE,
                entity_type TEXT NOT NULL DEFAULT 'unknown',
                scope TEXT NOT NULL DEFAULT 'global',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE entity_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                alias TEXT NOT NULL,
                variant_key TEXT NOT NULL DEFAULT '',
                original_form TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(entity_id, variant_key)
            );
            INSERT INTO entities VALUES
                (7, 'MemoryMaster', 'project', 'project:memorymaster',
                 '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
            """
        )
        migration.apply_sqlite(conn)
        assert conn.execute(
            "SELECT id, canonical_name FROM entities"
        ).fetchall() == [(7, "MemoryMaster")]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    EntityGraph(str(db_path), read_only=True).assert_ready()


def test_missing_graph_schema_fails_explicitly_without_ddl(tmp_path: Path) -> None:
    db_path = tmp_path / "not-ready.db"
    sqlite3.connect(db_path).close()

    with pytest.raises(EntityGraphNotReady, match="init-db"):
        EntityGraph(str(db_path), read_only=True).get_stats()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall() == []


def test_entity_migration_declares_sqlite_postgres_parity() -> None:
    from memorymaster.stores.migrations import entity_schema_contract

    sqlite_contract = entity_schema_contract("sqlite")
    postgres_contract = entity_schema_contract("postgres")

    assert sqlite_contract == postgres_contract
    assert sqlite_contract == {
        "entities": ("id", "canonical_name", "entity_type", "scope"),
        "entity_aliases": ("id", "entity_id", "alias", "variant_key"),
        "entity_edges": ("source_id", "target_id", "relation", "claim_id"),
        "claim_entity_links": ("claim_id", "entity_id"),
    }


def test_postgres_migration_materializes_the_canonical_contract() -> None:
    statements: list[str] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement: str) -> None:
            statements.append(" ".join(statement.split()).lower())

    class _Connection:
        committed = False

        def cursor(self):
            return _Cursor()

        def commit(self) -> None:
            self.committed = True

    connection = _Connection()
    migration = import_module(
        "memorymaster.stores.migrations.0013_canonical_entity_graph"
    )
    migration.apply_postgres(connection)

    ddl = "\n".join(statements)
    assert connection.committed is True
    for table in (
        "entities",
        "entity_aliases",
        "entity_edges",
        "claim_entity_links",
    ):
        assert f"table if not exists {table}" in ddl
    assert "references entities(id)" in ddl
    assert "references claims(id)" in ddl


def test_mcp_entity_reads_never_call_schema_creation(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "mcp-entity.db"
    MemoryService(db_path, workspace_root=tmp_path).init_db()

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("read tool attempted schema creation")

    monkeypatch.setattr(EntityGraph, "ensure_tables", _forbidden)
    stats = mcp_server.entity_stats(db=str(db_path))
    related = mcp_server.find_related_claims("Unknown", db=str(db_path))

    assert stats == {
        "ok": True,
        "entities": 0,
        "edges": 0,
        "claim_links": 0,
        "by_type": {},
    }
    assert related == {"ok": True, "claim_ids": [], "count": 0}


def test_mcp_entity_readiness_failure_is_actionable(tmp_path: Path) -> None:
    db_path = tmp_path / "mcp-not-ready.db"
    sqlite3.connect(db_path).close()

    result = mcp_server.entity_stats(db=str(db_path))

    assert result["ok"] is False
    assert result["code"] == "ENTITY_GRAPH_NOT_READY"
    assert "init-db" in result["error"]


def test_init_extract_stats_related_and_enriched_recall(tmp_path: Path) -> None:
    db_path = tmp_path / "entity-e2e.db"
    service = MemoryService(db_path, workspace_root=tmp_path)
    service.init_db()
    alice = service.ingest(
        "Alice uses the canonical entity graph",
        [CitationInput(source="test://alice")],
    )
    bob = service.ingest(
        "Bob collaborates with Alice",
        [CitationInput(source="test://bob")],
    )
    payload = (
        '{"entities":[{"name":"Alice","type":"person","aliases":[]},'
        '{"name":"Bob","type":"person","aliases":[]}],'
        '"relations":[{"source":"Bob","target":"Alice","relation":"knows"}]}'
    )

    graph = EntityGraph(str(db_path))
    with patch("memorymaster.knowledge.entity_graph._llm_chat", return_value=payload):
        assert graph.extract_and_link(bob.id, bob.text) == ["Alice", "Bob"]

    assert graph.get_stats() == {
        "entities": 2,
        "edges": 1,
        "claim_links": 2,
        "by_type": {"person": 2},
    }
    assert bob.id in graph.find_related_claims(["Alice"], hops=1)
    rows = service.query_rows(
        "Alice",
        limit=10,
        include_candidates=True,
        enrich_with_entities=True,
    )
    assert {row["claim"].id for row in rows} >= {alice.id, bob.id}
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
