"""Migration, query-plan and disposable restore evidence for the catalog index."""
from __future__ import annotations

import importlib
import sqlite3

import pytest


MIGRATION = importlib.import_module("memorymaster.stores.migrations.0025_active_skill_catalog")


def test_skill_catalog_index_upgrade_idempotency_and_restore(tmp_path):
    database = tmp_path / "before.db"
    backup = tmp_path / "backup.db"
    with sqlite3.connect(database) as conn:
        conn.executescript("""
            CREATE TABLE claims (
                id INTEGER PRIMARY KEY, text TEXT, claim_type TEXT, status TEXT,
                tenant_id TEXT, scope TEXT, replaced_by_claim_id INTEGER
            );
            INSERT INTO claims VALUES(1,'ordinary','fact','confirmed','a','project:test',NULL);
            INSERT INTO claims VALUES(2,'skill','skill','confirmed','a','project:test',NULL);
        """)
        before = conn.execute("SELECT * FROM claims ORDER BY id").fetchall()
        with sqlite3.connect(backup) as saved:
            conn.backup(saved)
        MIGRATION.apply_sqlite(conn)
        MIGRATION.apply_sqlite(conn)
        assert conn.execute("SELECT * FROM claims ORDER BY id").fetchall() == before
        plan = conn.execute("""EXPLAIN QUERY PLAN SELECT id FROM claims
            WHERE claim_type='skill' AND status='confirmed' AND replaced_by_claim_id IS NULL
              AND tenant_id=? AND scope IN (?) AND id>? ORDER BY id LIMIT 256
            """, ("a", "project:test", 0)).fetchall()
        assert "idx_claims_active_skill_catalog" in str(plan)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        with sqlite3.connect(backup) as saved:
            saved.backup(conn)
        assert conn.execute("SELECT * FROM claims ORDER BY id").fetchall() == before
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_claims_active_skill_catalog'").fetchone() is None
        MIGRATION.apply_sqlite(conn)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_skill_catalog_migration_on_empty_database_then_init(tmp_path):
    from memorymaster.core.service import MemoryService

    database = tmp_path / "empty.db"
    with sqlite3.connect(database) as conn:
        MIGRATION.apply_sqlite(conn)
    service = MemoryService(database, workspace_root=tmp_path)
    service.init_db()
    with service.store.connect() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_claims_active_skill_catalog'").fetchone()


def test_skill_catalog_migration_postgres_is_explicitly_unsupported():
    with pytest.raises(RuntimeError, match="SQLite-only"):
        MIGRATION.apply_postgres(object())


def test_stale_database_init_upgrades_without_lenient_fallback(tmp_path, caplog):
    from memorymaster.core.service import MemoryService

    service = MemoryService(tmp_path / "stale.db", workspace_root=tmp_path)
    service.init_db()
    with service.store.connect() as conn:
        conn.execute("DROP INDEX idx_claims_active_skill_catalog")
        conn.execute("DELETE FROM schema_versions WHERE version=25")
    caplog.clear()
    service.init_db()
    assert "lenient" not in caplog.text.lower()
    with service.store.connect() as conn:
        assert conn.execute("SELECT 1 FROM schema_versions WHERE version=25").fetchone()
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_claims_active_skill_catalog'").fetchone()
