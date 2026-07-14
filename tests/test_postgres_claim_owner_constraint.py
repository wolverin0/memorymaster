"""Adversarial RED contracts for PostgreSQL claim-owner enforcement."""
from __future__ import annotations

import importlib
import re
from typing import Sequence

import pytest

from memorymaster.stores.postgres_store import PostgresStore


OWNER_CONSTRAINT_NAME = "ck_claims_identity_visibility_owner"
EXACT_OWNER_CHECK = (
    "CHECK (visibility IN ('public', 'private', 'sensitive') "
    "AND NULLIF(BTRIM(source_agent), '') IS NOT NULL)"
)


def _constraint_row(
    *,
    definition: str = EXACT_OWNER_CHECK,
    validated: bool = True,
) -> dict[str, object]:
    return {
        "schema_name": "public",
        "table_name": "claims",
        "constraint_name": OWNER_CONSTRAINT_NAME,
        "conname": OWNER_CONSTRAINT_NAME,
        "constraint_type": "c",
        "contype": "c",
        "validated": validated,
        "convalidated": validated,
        "is_local": True,
        "conislocal": True,
        "no_inherit": False,
        "connoinherit": False,
        "constraint_definition": definition,
        "definition": definition,
    }


class OwnerConstraintCursor:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self._rows: list[dict[str, object]] = []

    def __enter__(self) -> OwnerConstraintCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        self.executed.append((sql, tuple(params)))
        if "pg_constraint" in _canonical(sql):
            self._rows = [] if self.row is None else [dict(self.row)]
        else:
            self._rows = []

    def fetchone(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, object]]:
        return list(self._rows)


class OwnerConstraintConnection:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.autocommit = True
        self.closed = False
        self.rollback_count = 0
        self.cursor_instance = OwnerConstraintCursor(row)

    def cursor(self) -> OwnerConstraintCursor:
        return self.cursor_instance

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        self.closed = True


def _canonical(sql: str) -> str:
    return " ".join(sql.lower().replace('"', "").split())


def _team_connect(
    monkeypatch: pytest.MonkeyPatch,
    row: dict[str, object] | None,
) -> tuple[PostgresStore, OwnerConstraintConnection]:
    store = PostgresStore(
        "postgresql://runtime.invalid/memorymaster",
        tenant_id="tenant-a",
        require_tenant=True,
        principal="alice",
        allowed_scopes={"project:a"},
    )
    connection = OwnerConstraintConnection(row)
    monkeypatch.setattr(store, "_open_connection", lambda: connection)
    for method in (
        "_validate_runtime_role",
        "_validate_runtime_tables",
        "_validate_runtime_metadata_tables",
        "_validate_claim_identity_indexes",
        "_validate_claim_supersession_guard",
        "_validate_event_append_only_catalog",
        "_validate_event_chain_head_function",
        "_validate_runtime_migration",
        "_validate_runtime_policies",
    ):
        monkeypatch.setattr(
            PostgresStore,
            method,
            classmethod(lambda _cls, _cur: None),
        )
    monkeypatch.setattr(
        PostgresStore,
        "_bind_runtime_authority",
        classmethod(lambda _cls, _cur, _tenant, _principal, _scopes: None),
    )
    return store, connection


def test_team_runtime_accepts_exact_validated_source_owner_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, connection = _team_connect(monkeypatch, _constraint_row())

    assert store.connect() is connection
    assert connection.closed is False


@pytest.mark.parametrize(
    "row",
    [
        None,
        _constraint_row(validated=False),
        _constraint_row(
            definition=(
                "CHECK (visibility IN ('public', 'private', 'sensitive') AND "
                "(visibility = 'public' OR "
                "NULLIF(BTRIM(source_agent), '') IS NOT NULL))"
            )
        ),
    ],
    ids=("missing", "not-validated", "public-owner-bypass"),
)
def test_team_runtime_rejects_unsafe_source_owner_constraint_before_binding(
    monkeypatch: pytest.MonkeyPatch,
    row: dict[str, object] | None,
) -> None:
    store, connection = _team_connect(monkeypatch, row)

    with pytest.raises(PermissionError, match="(?i)(claim|owner|constraint|validated)"):
        store.connect()

    assert connection.rollback_count == 1
    assert connection.closed is True


def test_team_runtime_inspects_exact_constraint_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, connection = _team_connect(monkeypatch, _constraint_row())

    store.connect()

    catalog_sql = "\n".join(sql for sql, _params in connection.cursor_instance.executed)
    normalized = _canonical(catalog_sql)
    assert "pg_constraint" in normalized
    assert "pg_get_constraintdef" in normalized
    assert "convalidated" in normalized


def test_v0012_source_owner_constraint_has_no_public_owner_bypass() -> None:
    migration = importlib.import_module(
        "memorymaster.stores.migrations.0012_principal_local_claim_identities"
    )
    ddl = _canonical(migration._POSTGRES_DDL)

    assert "nullif(btrim(source_agent), '') is not null" in ddl
    assert not re.search(
        r"visibility\s*=\s*'public'\s+or\s+nullif\(btrim\(source_agent\)",
        ddl,
    )


def test_brownfield_owner_preflight_is_read_only_and_includes_public_rows() -> None:
    migration = importlib.import_module(
        "memorymaster.stores.migrations.0012_principal_local_claim_identities"
    )
    sql = _canonical(getattr(migration, "POSTGRES_IDENTITY_PREFLIGHT_SQL", ""))

    assert sql.startswith("select")
    assert "visibility" in sql
    assert "nullif(btrim(source_agent), '') is null" in sql
    assert "visibility <> 'public'" not in sql
    assert "visibility != 'public'" not in sql
    assert not re.search(r"\b(insert|update|delete|alter|drop|create|truncate)\b", sql)
