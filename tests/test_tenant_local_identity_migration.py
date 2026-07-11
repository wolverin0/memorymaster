"""Schema-contract tests for tenant-local claim identities."""
from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

from memorymaster.stores.migrations import discover_migrations
from memorymaster.stores._storage_schema import _SchemaMixin
from memorymaster.stores.storage import SQLiteStore


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


def _migration():
    return importlib.import_module(
        "memorymaster.stores.migrations.0009_tenant_local_claim_identities"
    )


def test_tenant_local_identity_migration_is_versioned() -> None:
    migration = next(item for item in discover_migrations() if item.version == 9)
    assert "tenant" in migration.description.lower()
    assert "identit" in migration.description.lower()


def test_sqlite_migration_replaces_global_identity_constraints() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE claims (
            id INTEGER PRIMARY KEY,
            idempotency_key TEXT,
            human_id TEXT,
            subject TEXT,
            predicate TEXT,
            scope TEXT,
            status TEXT,
            tenant_id TEXT
        );
        CREATE UNIQUE INDEX idx_claims_idempotency_key ON claims(idempotency_key);
        CREATE UNIQUE INDEX idx_claims_human_id ON claims(human_id);
        CREATE UNIQUE INDEX idx_claims_confirmed_tuple_unique
            ON claims(subject, predicate, scope) WHERE status = 'confirmed';
        """
    )
    try:
        _migration().apply_sqlite(conn)
        indexes = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        triggers = "\n".join(
            row[0]
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        )
    finally:
        conn.close()

    assert "idx_claims_tenant_idempotency_key" in indexes
    assert "idx_claims_tenant_human_id" in indexes
    assert "idx_claims_confirmed_tuple_unique" in indexes
    assert "COALESCE(tenant_id, '')" in indexes["idx_claims_tenant_idempotency_key"]
    assert "tenant_id IS NEW.tenant_id" in triggers


def test_postgres_migration_uses_null_safe_tenant_identity() -> None:
    conn = RecordingConnection()
    _migration().apply_postgres(conn)
    emitted = "\n".join(conn.statements)

    assert "idx_claims_tenant_idempotency_key" in emitted
    assert "idx_claims_tenant_human_id" in emitted
    assert "idx_claims_confirmed_tuple_unique" in emitted
    assert "c.tenant_id IS NOT DISTINCT FROM NEW.tenant_id" in emitted
    assert "UPDATE OF status, subject, predicate, scope, tenant_id" in emitted
    assert conn.commits == 1


def test_bootstrap_schemas_declare_tenant_local_identity_indexes() -> None:
    root = Path(__file__).resolve().parents[1] / "memorymaster"
    sqlite_schema = (root / "schema.sql").read_text(encoding="utf-8")
    postgres_schema = (root / "schema_postgres.sql").read_text(encoding="utf-8")

    for name in (
        "idx_claims_public_idempotency_key_unique",
        "idx_claims_nonpublic_principal_idempotency_key_unique",
        "idx_claims_public_human_id_unique",
        "idx_claims_nonpublic_principal_human_id_unique",
        "idx_claims_public_confirmed_tuple_unique",
        "idx_claims_nonpublic_principal_confirmed_tuple_unique",
    ):
        assert name in sqlite_schema
        assert name in postgres_schema
    assert "ON claims(COALESCE(tenant_id, ''), subject, predicate, scope)" in sqlite_schema
    assert "ON claims(COALESCE(tenant_id, ''), subject, predicate, scope)" in postgres_schema


def test_sqlite_reinit_does_not_restore_global_unique_indexes(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "reinit.db")
    store.init_db()
    store.init_db()

    with store.connect() as conn:
        indexes = {
            row[1]: bool(row[2])
            for row in conn.execute("PRAGMA index_list(claims)").fetchall()
        }

    assert indexes["idx_claims_idempotency_key"] is False
    assert indexes["idx_claims_human_id"] is False
    identity_indexes = {
        name for name, unique in indexes.items() if unique and name.startswith("idx_claims_")
    }
    assert identity_indexes == {
        "idx_claims_public_idempotency_key_unique",
        "idx_claims_nonpublic_principal_idempotency_key_unique",
        "idx_claims_public_human_id_unique",
        "idx_claims_nonpublic_principal_human_id_unique",
        "idx_claims_public_confirmed_tuple_unique",
        "idx_claims_nonpublic_principal_confirmed_tuple_unique",
    }


def test_legacy_human_id_index_is_converted_before_backfill() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE claims (
            id INTEGER PRIMARY KEY,
            subject TEXT,
            text TEXT NOT NULL,
            human_id TEXT,
            tenant_id TEXT
        );
        CREATE TABLE claim_links (
            source_id INTEGER,
            target_id INTEGER,
            link_type TEXT
        );
        CREATE UNIQUE INDEX idx_claims_human_id ON claims(human_id);
        INSERT INTO claims(id, subject, text, tenant_id) VALUES
            (1, 'same', 'same text', 'tenant-a'),
            (2, 'same', 'same text', 'tenant-b');
        """
    )
    try:
        updated = _SchemaMixin._ensure_human_id_schema(conn)
        rows = conn.execute(
            "SELECT tenant_id, human_id FROM claims ORDER BY tenant_id"
        ).fetchall()
    finally:
        conn.close()

    assert updated is None
    assert rows[0]["human_id"] == rows[1]["human_id"]
