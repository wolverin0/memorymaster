"""Adversarial contract tests for the PostgreSQL tenant-RLS migration."""
from __future__ import annotations

import importlib
import sqlite3

from memorymaster.stores.migrations import discover_migrations


TENANT_POLICY_TABLES = {
    "claims",
    "citations",
    "events",
    "claim_links",
    "claim_embeddings",
    "action_proposals",
    "contradiction_verdicts",
    "mcp_usage",
}

TEAM_DENY_TABLES = {
    "external_sources",
    "source_items",
    "evidence_items",
    "media_retry_queue",
    "query_cache",
    "miner_state",
    "rule_stats",
}


class RecordingCursor:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, _params: object = None) -> None:
        self.statements.append(" ".join(sql.split()))


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.commits = 0

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self.statements)

    def commit(self) -> None:
        self.commits += 1


def _apply_postgres_migration() -> RecordingConnection:
    migration = importlib.import_module(
        "memorymaster.stores.migrations.0008_postgres_tenant_rls"
    )
    conn = RecordingConnection()
    migration.apply_postgres(conn)
    return conn


def test_postgres_tenant_rls_is_versioned() -> None:
    migration = next(item for item in discover_migrations() if item.version == 8)

    assert "tenant" in migration.description.lower()
    assert "row" in migration.description.lower()


def test_sqlite_side_of_postgres_rls_migration_is_noop() -> None:
    migration = importlib.import_module(
        "memorymaster.stores.migrations.0008_postgres_tenant_rls"
    )
    conn = sqlite3.connect(":memory:")
    try:
        migration.apply_sqlite(conn)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        conn.close()

    assert tables == []


def test_tenant_policy_cannot_be_widened_by_permissive_policy() -> None:
    conn = _apply_postgres_migration()
    emitted = "\n".join(conn.statements)

    for table in TENANT_POLICY_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in emitted
        assert f"CREATE POLICY memorymaster_tenant_restrict ON {table}" in emitted
    assert emitted.count("AS RESTRICTIVE") >= len(TENANT_POLICY_TABLES)
    assert "current_setting('memorymaster.tenant_id', true)" in emitted
    assert "USING" in emitted
    assert "WITH CHECK" in emitted
    assert conn.commits == 1


def test_missing_tenant_cannot_match_tenantless_rows() -> None:
    emitted = "\n".join(_apply_postgres_migration().statements)

    assert (
        "tenant_id = NULLIF(current_setting('memorymaster.tenant_id', true), '')"
        in emitted
    )
    assert "tenant_id IS NOT DISTINCT FROM" not in emitted


def test_untenantable_team_tables_are_explicitly_denied() -> None:
    emitted = "\n".join(_apply_postgres_migration().statements)

    for table in TEAM_DENY_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in emitted
        assert f"CREATE POLICY memorymaster_team_deny ON {table}" in emitted
    assert emitted.count("USING (FALSE) WITH CHECK (FALSE)") >= len(TEAM_DENY_TABLES)
