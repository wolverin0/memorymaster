"""Adversarial contracts for the PostgreSQL tenant-global event chain head.

The event ledger is one primary and one secondary hash chain per tenant.  RLS
may hide events from another scope or private principal, so an application
query over ``events`` cannot be trusted to discover either chain head.
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, replace

import pytest

from memorymaster.stores.postgres_store import (
    POSTGRES_TENANT_EVENT_HASH_ALGO,
    PostgresStore,
)


class MigrationCursor:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    def __enter__(self) -> "MigrationCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: object, _params: object = None) -> None:
        self.statements.append(str(statement).strip())


class MigrationConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> MigrationCursor:
        return MigrationCursor(self.statements)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _migration_statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    migration = importlib.import_module(
        "memorymaster.stores.migrations.0011_postgres_scoped_force_rls"
    )
    monkeypatch.setattr(migration, "_stamp_policy_manifest", lambda _cur: None)
    connection = MigrationConnection()

    migration.apply_postgres(connection)

    assert connection.commits == 1
    assert connection.rollbacks == 0
    return connection.statements


def _event_head_function(statements: list[str]) -> str:
    matches = [
        statement
        for statement in statements
        if re.search(
            r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+"
            r"public\.memorymaster_event_chain_head\s*\(\s*\)",
            statement,
            flags=re.IGNORECASE,
        )
    ]
    assert len(matches) == 1
    return matches[0]


def test_v0011_installs_narrow_security_definer_event_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function_sql = _event_head_function(_migration_statements(monkeypatch))
    normalized = " ".join(function_sql.split()).lower()

    assert "security definer" in normalized
    assert re.search(
        r"set\s+search_path\s*(?:=|to)\s*pg_catalog\s*,\s*pg_temp",
        normalized,
    )
    assert "from public.events" in normalized
    assert "current_setting('memorymaster.tenant_id', true)" in normalized
    assert "memorymaster.principal" not in normalized
    assert "memorymaster.allowed_scopes" not in normalized
    assert "payload_json" not in normalized
    assert "details" not in normalized

    returns = re.search(
        r"returns\s+table\s*\((.*?)\)",
        normalized,
    )
    assert returns is not None
    returned_columns = [part.strip().split()[0] for part in returns.group(1).split(",")]
    assert len(returned_columns) == 2
    assert any(name in {"event_hash", "global_event_hash"} for name in returned_columns)
    assert "tenant_event_hash" in returned_columns


def test_v0011_revokes_public_event_head_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted = "\n".join(_migration_statements(monkeypatch))
    normalized = " ".join(emitted.split()).upper()

    assert re.search(
        r"REVOKE\s+(?:ALL|EXECUTE)\s+ON\s+FUNCTION\s+"
        r"PUBLIC\.MEMORYMASTER_EVENT_CHAIN_HEAD\s*\(\s*\)\s+FROM\s+PUBLIC",
        normalized,
    )
    assert not re.search(
        r"GRANT\s+EXECUTE\s+ON\s+FUNCTION\s+"
        r"PUBLIC\.MEMORYMASTER_EVENT_CHAIN_HEAD\s*\(\s*\)\s+TO\s+PUBLIC",
        normalized,
    )


class RlsFilteredHeadCursor:
    """Simulate two RLS views over one tenant-global event ledger."""

    def __init__(self, filtered_head: str, filtered_tenant_head: str) -> None:
        self.filtered_head = filtered_head
        self.filtered_tenant_head = filtered_tenant_head
        self._row: dict[str, str | None] | None = None
        self.function_calls = 0
        self.direct_event_selects = 0

    def execute(self, statement: str, _params: object = None) -> None:
        normalized = " ".join(statement.split()).lower()
        if "pg_advisory_xact_lock" in normalized:
            self._row = None
            return
        if "memorymaster_event_chain_head()" in normalized:
            self.function_calls += 1
            self._row = {
                "global_event_hash": "tenant-global-primary-head",
                "event_hash": "tenant-global-primary-head",
                "tenant_event_hash": "tenant-global-secondary-head",
            }
            return
        if "from events" in normalized or "from public.events" in normalized:
            self.direct_event_selects += 1
            key = "tenant_event_hash" if "tenant_event_hash" in normalized else "event_hash"
            value = self.filtered_tenant_head if key == "tenant_event_hash" else self.filtered_head
            self._row = {key: value}
            return
        raise AssertionError(f"unexpected event-head SQL: {normalized}")

    def fetchone(self) -> dict[str, str | None] | None:
        return self._row


def _team_store(*, principal: str, scope: str) -> PostgresStore:
    return PostgresStore(
        "postgresql://unused",
        tenant_id="tenant-a",
        require_tenant=True,
        principal=principal,
        allowed_scopes=(scope,),
    )


def test_event_head_is_identical_across_scope_and_private_principal_views() -> None:
    alice = _team_store(principal="alice", scope="project:a")
    bob = _team_store(principal="bob", scope="project:b")
    alice_cursor = RlsFilteredHeadCursor("alice-visible", "alice-private-visible")
    bob_cursor = RlsFilteredHeadCursor("bob-visible", "bob-private-visible")

    alice_head = alice._event_chain_head(alice_cursor, "tenant-a")
    bob_head = bob._event_chain_head(bob_cursor, "tenant-a")

    expected = (
        "tenant-global-primary-head",
        POSTGRES_TENANT_EVENT_HASH_ALGO,
        "tenant-global-secondary-head",
    )
    assert alice_head == expected
    assert bob_head == expected
    assert alice_cursor.function_calls == bob_cursor.function_calls == 1
    assert alice_cursor.direct_event_selects == bob_cursor.direct_event_selects == 0


_EVENT_HEAD_MIGRATION = importlib.import_module(
    "memorymaster.stores.migrations.0011_postgres_scoped_force_rls"
)
_EVENT_HEAD_DEFINITION = str(_EVENT_HEAD_MIGRATION._EVENT_HEAD_FUNCTION)
_EVENT_HEAD_SOURCE = _EVENT_HEAD_DEFINITION.split(
    "AS $$", 1
)[1].rsplit("$$", 1)[0].strip()


@dataclass(frozen=True)
class FunctionCatalogState:
    schema_name: str = "public"
    function_name: str = "memorymaster_event_chain_head"
    argument_count: int = 0
    result_signature: str = "TABLE(global_event_hash text, tenant_event_hash text)"
    language_name: str = "plpgsql"
    security_definer: bool = True
    function_config: tuple[str, ...] = ("search_path=pg_catalog, pg_temp",)
    volatility: str = "v"
    parallel_safety: str = "u"
    leakproof: bool = False
    strict: bool = False
    public_execute: bool = False
    runtime_execute: bool = True
    owner_is_runtime: bool = False
    owner_member: bool = False
    owner_superuser: bool = True
    owner_bypassrls: bool = False
    function_source: str = _EVENT_HEAD_SOURCE
    function_definition: str = _EVENT_HEAD_DEFINITION


class FunctionCatalogCursor:
    def __init__(self, state: FunctionCatalogState) -> None:
        self.state = state
        self.statements: list[str] = []

    def execute(self, statement: str, _params: object = None) -> None:
        self.statements.append(" ".join(statement.split()))

    def fetchone(self) -> dict[str, object]:
        return vars(self.state)


def test_runtime_accepts_only_exact_event_head_capability() -> None:
    cursor = FunctionCatalogCursor(FunctionCatalogState())

    PostgresStore._validate_event_chain_head_function(cursor)

    assert any("pg_proc" in statement.lower() for statement in cursor.statements)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"argument_count": 1}, "argument|signature"),
        ({"security_definer": False}, "security.definer"),
        ({"function_config": ()}, "search.path"),
        ({"function_config": ("search_path=public",)}, "search.path"),
        ({"public_execute": True}, "public|execute"),
        ({"runtime_execute": False}, "execute|privilege"),
        ({"owner_is_runtime": True}, "owner|runtime"),
        (
            {"owner_superuser": False, "owner_bypassrls": False},
            "owner|bypass|rls",
        ),
        (
            {
                "function_source": "BEGIN RETURN QUERY SELECT event_hash, "
                "tenant_event_hash FROM public.events; END;"
            },
            "body|definition|events",
        ),
    ],
)
def test_runtime_rejects_event_head_catalog_drift(
    change: dict[str, object],
    match: str,
) -> None:
    cursor = FunctionCatalogCursor(replace(FunctionCatalogState(), **change))

    with pytest.raises(PermissionError, match=f"(?i)({match})"):
        PostgresStore._validate_event_chain_head_function(cursor)


def test_runtime_preserves_literal_case_in_event_head_fingerprint() -> None:
    drifted_source = _EVENT_HEAD_SOURCE.replace(
        "'sha256-tenant-v2'",
        "'SHA256-TENANT-V2'",
    )
    assert drifted_source != _EVENT_HEAD_SOURCE
    cursor = FunctionCatalogCursor(
        replace(FunctionCatalogState(), function_source=drifted_source)
    )

    with pytest.raises(PermissionError, match="(?i)(body|definition|events)"):
        PostgresStore._validate_event_chain_head_function(cursor)
