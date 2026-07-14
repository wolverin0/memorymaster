"""RED contracts for v0012 principal-local claim identities.

Public identities are tenant-wide.  Every non-public visibility is a separate
principal-local namespace, so ``private`` and ``sensitive`` must not alias one
another even for the same principal.
"""
from __future__ import annotations

import hashlib
import importlib
import re
import sqlite3
from pathlib import Path

from memorymaster.stores.migrations import discover_migrations
from memorymaster.stores.storage import SQLiteStore


PUBLIC_INDEXES = {
    "idx_claims_public_idempotency_key_unique",
    "idx_claims_public_human_id_unique",
    "idx_claims_public_confirmed_tuple_unique",
}
NONPUBLIC_INDEXES = {
    "idx_claims_nonpublic_principal_idempotency_key_unique",
    "idx_claims_nonpublic_principal_human_id_unique",
    "idx_claims_nonpublic_principal_confirmed_tuple_unique",
}
IDENTITY_INDEXES = PUBLIC_INDEXES | NONPUBLIC_INDEXES
LEGACY_UNIQUE_INDEXES = {
    "idx_claims_tenant_idempotency_key",
    "idx_claims_tenant_human_id",
    "idx_claims_confirmed_tuple_unique",
}
LEGACY_GUARDS = {
    "trg_claims_confirmed_tuple_guard",
    "trg_claims_confirmed_tuple_guard_insert",
    "trg_claims_confirmed_tuple_guard_update",
}


def _migration():
    return importlib.import_module(
        "memorymaster.stores.migrations.0012_principal_local_claim_identities"
    )


def _canonical(sql: str | None) -> str:
    return " ".join((sql or "").lower().replace('"', "").split())


def _claim_indexes(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): _canonical(row[1])
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'claims' AND sql IS NOT NULL"
        ).fetchall()
    }


def _unique_claim_indexes(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute("PRAGMA index_list(claims)").fetchall()
        if bool(row[2])
    }


def _claim_triggers(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = 'claims'"
        ).fetchall()
    }


def _legacy_sqlite_claims() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE claims (
            id INTEGER PRIMARY KEY,
            idempotency_key TEXT,
            human_id TEXT,
            subject TEXT,
            predicate TEXT,
            scope TEXT NOT NULL,
            status TEXT NOT NULL,
            tenant_id TEXT,
            source_agent TEXT,
            visibility TEXT NOT NULL DEFAULT 'public'
        );
        CREATE UNIQUE INDEX idx_claims_tenant_idempotency_key
            ON claims(COALESCE(tenant_id, ''), idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE UNIQUE INDEX idx_claims_tenant_human_id
            ON claims(COALESCE(tenant_id, ''), human_id)
            WHERE human_id IS NOT NULL;
        CREATE UNIQUE INDEX idx_claims_confirmed_tuple_unique
            ON claims(COALESCE(tenant_id, ''), subject, predicate, scope)
            WHERE status = 'confirmed'
              AND subject IS NOT NULL AND predicate IS NOT NULL;
        CREATE TRIGGER trg_claims_confirmed_tuple_guard_insert
        BEFORE INSERT ON claims BEGIN SELECT 1; END;
        CREATE TRIGGER trg_claims_confirmed_tuple_guard_update
        BEFORE UPDATE ON claims BEGIN SELECT 1; END;
        """
    )
    return conn


def _assert_exact_principal_identity_catalog(conn: sqlite3.Connection) -> None:
    indexes = _claim_indexes(conn)
    assert _unique_claim_indexes(conn) == IDENTITY_INDEXES
    assert LEGACY_UNIQUE_INDEXES.isdisjoint(indexes)
    assert LEGACY_GUARDS.isdisjoint(_claim_triggers(conn))

    for name in PUBLIC_INDEXES:
        sql = indexes[name]
        assert "visibility = 'public'" in sql
        assert "source_agent" not in sql.split(" where ", 1)[0]

    for name in NONPUBLIC_INDEXES:
        sql = indexes[name]
        key_sql, predicate_sql = sql.split(" where ", 1)
        assert re.search(r"\bvisibility\b.*\bsource_agent\b", key_sql)
        assert "visibility <> 'public'" in predicate_sql
        assert "source_agent is not null" in predicate_sql

    for token in ("idempotency_key", "human_id"):
        public = indexes[f"idx_claims_public_{token}_unique"]
        nonpublic = indexes[f"idx_claims_nonpublic_principal_{token}_unique"]
        assert f"{token} is not null" in public
        assert f"{token} is not null" in nonpublic

    for name in (
        "idx_claims_public_confirmed_tuple_unique",
        "idx_claims_nonpublic_principal_confirmed_tuple_unique",
    ):
        sql = indexes[name]
        assert all(token in sql for token in ("subject", "predicate", "scope"))
        assert "status = 'confirmed'" in sql
        assert "subject is not null" in sql
        assert "predicate is not null" in sql


class RecordingCursor:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    def __enter__(self) -> RecordingCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, _params: object = None) -> None:
        self.statements.append(_canonical(sql))

    def fetchone(self) -> dict[str, int]:
        return {"invalid_supersession_edges": 0}


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.commits = 0

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self.statements)

    def commit(self) -> None:
        self.commits += 1


def test_v0012_is_discoverable_and_checksum_is_source_frozen() -> None:
    migration = next(item for item in discover_migrations() if item.version == 12)

    assert migration.module_name.endswith("0012_principal_local_claim_identities")
    assert "principal" in migration.description.lower()
    assert migration.checksum() == hashlib.sha256(
        migration.source_path.read_bytes()
    ).hexdigest()


def test_v0012_sqlite_replaces_v9_globals_with_exact_six_indexes() -> None:
    conn = _legacy_sqlite_claims()
    try:
        _migration().apply_sqlite(conn)
        _assert_exact_principal_identity_catalog(conn)
    finally:
        conn.close()


def test_v0012_postgres_drops_v9_guards_and_builds_exact_six_indexes() -> None:
    conn = RecordingConnection()

    _migration().apply_postgres(conn)

    emitted = "\n".join(conn.statements)
    created = {
        match.group(1)
        for match in re.finditer(
            r"create unique index(?: if not exists)? ([a-z0-9_]+)", emitted
        )
    }
    assert created == IDENTITY_INDEXES
    for name in LEGACY_UNIQUE_INDEXES:
        assert f"drop index if exists {name}" in emitted
    assert "drop trigger if exists trg_claims_confirmed_tuple_guard on claims" in emitted
    assert "drop function if exists memorymaster_claims_confirmed_tuple_guard()" in emitted
    assert "visibility = 'public'" in emitted
    assert "visibility <> 'public'" in emitted
    assert "source_agent is not null" in emitted
    assert conn.commits == 1


def test_bootstrap_schemas_declare_v12_not_v9_identity_constraints() -> None:
    root = Path(__file__).resolve().parents[1] / "memorymaster"
    for schema_name in ("schema.sql", "schema_postgres.sql"):
        schema = _canonical((root / schema_name).read_text(encoding="utf-8"))
        for name in IDENTITY_INDEXES:
            assert name in schema
        for name in LEGACY_UNIQUE_INDEXES | LEGACY_GUARDS:
            assert name not in schema


def test_recurring_sqlite_init_never_recreates_v9_identity_constraints(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "principal-identities.db")
    store.init_db()
    store.init_db()

    with store.connect() as conn:
        _assert_exact_principal_identity_catalog(conn)


def test_legacy_ensure_helpers_cannot_restore_v9_after_v0012(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "ensure-contract.db")
    store.init_db()
    with store.connect() as conn:
        store._ensure_claim_idempotency_schema(conn)
        store._ensure_human_id_schema(conn)
        store._ensure_confirmed_tuple_uniqueness_schema(conn)
        conn.commit()
        _assert_exact_principal_identity_catalog(conn)
