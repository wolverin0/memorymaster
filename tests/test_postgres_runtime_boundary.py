from __future__ import annotations

import hashlib
import importlib
import json
import re
from dataclasses import dataclass, field
from typing import Sequence

import pytest

import memorymaster.stores._storage_schema as schema_module
from memorymaster.stores.migrations import discover_migrations
from memorymaster.stores.postgres_store import (
    POSTGRES_CLAIM_OWNER_CHECK,
    POSTGRES_EVENT_GUARD_SOURCE,
    PostgresStore,
)


TENANT_TABLES = (
    "claims",
    "citations",
    "events",
    "claim_links",
    "claim_embeddings",
    "contradiction_verdicts",
    "mcp_usage",
)
TEAM_DENY_TABLES = (
    "action_proposals",
    "external_sources",
    "source_items",
    "evidence_items",
    "media_retry_queue",
    "query_cache",
    "miner_state",
    "rule_stats",
)
PROTECTED_TABLES = TENANT_TABLES + TEAM_DENY_TABLES
AUTHORITY_GUCS = (
    "memorymaster.tenant_id",
    "memorymaster.principal",
    "memorymaster.allowed_scopes",
)
COMMAND_POLICIES = {
    "SELECT": "memorymaster_tenant_select",
    "INSERT": "memorymaster_tenant_insert",
    "UPDATE": "memorymaster_tenant_update",
    "DELETE": "memorymaster_tenant_delete",
}
PERMIT_POLICIES = {
    command: f"{name}_permit" for command, name in COMMAND_POLICIES.items()
}
POLICY_FIELDS = (
    "schemaname",
    "tablename",
    "policyname",
    "permissive",
    "roles",
    "cmd",
    "qual",
    "with_check",
)


def _canonical_policy_payload(policies: Sequence[dict[str, object]]) -> str:
    rows: list[dict[str, object]] = []
    for policy in policies:
        row = {field: policy.get(field) for field in POLICY_FIELDS}
        roles = row["roles"]
        if isinstance(roles, (list, tuple, set, frozenset)):
            row["roles"] = sorted(str(role) for role in roles)
        rows.append(row)
    rows.sort(
        key=lambda row: (
            str(row["schemaname"]),
            str(row["tablename"]),
            str(row["policyname"]),
        )
    )
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def _policy_manifest_comment(policies: Sequence[dict[str, object]]) -> str:
    payload = _canonical_policy_payload(policies).encode("utf-8")
    return (
        "memorymaster.rls/v1;manifest=0011;sha256="
        f"{hashlib.sha256(payload).hexdigest()}"
    )


def _v0011_checksum() -> str:
    migration = next(item for item in discover_migrations() if item.version == 11)
    return migration.checksum()


def _v0012_checksum() -> str:
    migration = next(item for item in discover_migrations() if item.version == 12)
    return migration.checksum()


def _table_row(table: str) -> dict[str, object]:
    return {
        "table_name": table,
        "relname": table,
        "relrowsecurity": True,
        "relforcerowsecurity": True,
        "owner_name": "memorymaster_admin",
        "table_owner": "memorymaster_admin",
        "owner_member": False,
        "is_owner_member": False,
        "can_truncate": False,
        "has_truncate": False,
        "can_references": False,
        "has_references": False,
        "can_trigger": False,
        "has_trigger": False,
        "can_select": table == "events",
        "has_select": table == "events",
        "can_insert": table == "events",
        "has_insert": table == "events",
        "can_update": False,
        "has_update": False,
        "can_update_any_column": False,
        "has_update_any_column": False,
        "can_delete": False,
        "has_delete": False,
    }


def _metadata_table_row(table: str) -> dict[str, object]:
    return {
        "table_name": table,
        "relname": table,
        "can_select": True,
        "has_select": True,
        "can_insert": False,
        "has_insert": False,
        "can_update": False,
        "has_update": False,
        "can_delete": False,
        "has_delete": False,
        "can_truncate": False,
        "has_truncate": False,
        "can_references": False,
        "has_references": False,
        "can_trigger": False,
        "has_trigger": False,
    }


def _claim_identity_index_rows() -> list[dict[str, object]]:
    return [
        {
            "index_name": name,
            "relname": name,
            "indisunique": True,
            "is_unique": True,
            "indisvalid": True,
            "is_valid": True,
            "indisready": True,
            "is_ready": True,
            "indexdef": definition,
            "index_definition": definition,
            "predicate": predicate,
            "index_predicate": predicate,
        }
        for name, (definition, predicate) in sorted(
            PostgresStore._expected_claim_identity_catalog().items()
        )
    ]


def _event_head_function_row() -> dict[str, object]:
    migration = importlib.import_module(
        "memorymaster.stores.migrations.0011_postgres_scoped_force_rls"
    )
    definition = str(migration._EVENT_HEAD_FUNCTION)
    source = definition.split("AS $$", 1)[1].rsplit("$$", 1)[0].strip()
    return {
        "schema_name": "public",
        "function_name": "memorymaster_event_chain_head",
        "argument_count": 0,
        "result_signature": "TABLE(global_event_hash text, tenant_event_hash text)",
        "language_name": "plpgsql",
        "security_definer": True,
        "function_config": ("search_path=pg_catalog, pg_temp",),
        "volatility": "v",
        "parallel_safety": "u",
        "leakproof": False,
        "strict": False,
        "public_execute": False,
        "runtime_execute": True,
        "owner_is_runtime": False,
        "owner_member": False,
        "owner_superuser": True,
        "owner_bypassrls": False,
        "function_source": source,
        "function_definition": definition,
    }


def _owner_constraint_row() -> dict[str, object]:
    return {
        "schema_name": "public",
        "table_name": "claims",
        "constraint_name": "ck_claims_identity_visibility_owner",
        "constraint_type": "c",
        "validated": True,
        "is_local": True,
        "no_inherit": False,
        "constraint_definition": POSTGRES_CLAIM_OWNER_CHECK,
    }


def _event_trigger_rows() -> list[dict[str, object]]:
    return [
        {
            "trigger_name": f"trg_events_append_only_{operation}",
            "table_schema": "public",
            "table_name": "events",
            "enabled_code": "O",
            "is_internal": False,
            "function_schema": "public",
            "function_name": "memorymaster_events_append_only_guard",
            "trigger_definition": (
                f"CREATE TRIGGER trg_events_append_only_{operation} BEFORE "
                f"{operation.upper()} ON public.events FOR EACH ROW EXECUTE FUNCTION "
                "public.memorymaster_events_append_only_guard()"
            ),
        }
        for operation in ("update", "delete")
    ]


def _event_guard_row() -> dict[str, object]:
    return {
        "schema_name": "public",
        "function_name": "memorymaster_events_append_only_guard",
        "argument_count": 0,
        "result_signature": "trigger",
        "language_name": "plpgsql",
        "security_definer": False,
        "function_config": (),
        "volatility": "v",
        "parallel_safety": "u",
        "leakproof": False,
        "strict": False,
        "owner_member": False,
        "function_source": POSTGRES_EVENT_GUARD_SOURCE,
    }


def _supersession_trigger_row() -> dict[str, object]:
    return {
        "trigger_name": "trg_claims_supersession_boundary",
        "table_schema": "public",
        "table_name": "claims",
        "enabled_code": "O",
        "is_internal": False,
        "function_schema": "public",
        "function_name": "memorymaster_claim_supersession_guard",
        "trigger_definition": (
            "CREATE TRIGGER trg_claims_supersession_boundary BEFORE INSERT OR "
            "UPDATE OF tenant_id, scope, visibility, source_agent, "
            "supersedes_claim_id, replaced_by_claim_id ON public.claims "
            "FOR EACH ROW EXECUTE FUNCTION "
            "public.memorymaster_claim_supersession_guard()"
        ),
    }


def _supersession_guard_row() -> dict[str, object]:
    migration = importlib.import_module(
        "memorymaster.stores.migrations.0012_principal_local_claim_identities"
    )
    definition = str(migration._SUPERSESSION_GUARD_FUNCTION)
    source = definition.split("AS $$", 1)[1].rsplit("$$", 1)[0].strip()
    return {
        "schema_name": "public",
        "function_name": "memorymaster_claim_supersession_guard",
        "argument_count": 0,
        "result_signature": "trigger",
        "language_name": "plpgsql",
        "security_definer": False,
        "function_config": (),
        "volatility": "v",
        "parallel_safety": "u",
        "leakproof": False,
        "strict": False,
        "owner_member": False,
        "function_source": source,
    }


def _tenant_predicate(table: str, command: str) -> str:
    migration = importlib.import_module(
        "memorymaster.stores.migrations.0011_postgres_scoped_force_rls"
    )
    if command == "SELECT":
        return migration._READ_PREDICATES[table]
    return migration._WRITE_PREDICATES[table]


def _policy_row(
    table: str,
    name: str,
    *,
    restrictive: bool,
    command: str = "ALL",
) -> dict[str, object]:
    if name == "memorymaster_team_deny":
        predicate = "FALSE"
    elif name in {*COMMAND_POLICIES.values(), *PERMIT_POLICIES.values()}:
        predicate = _tenant_predicate(table, command)
    else:
        predicate = "TRUE"
    qual = None if command == "INSERT" else predicate
    with_check = None if command in {"SELECT", "DELETE"} else predicate
    return {
        "schemaname": "public",
        "tablename": table,
        "table_name": table,
        "policyname": name,
        "policy_name": name,
        "permissive": "RESTRICTIVE" if restrictive else "PERMISSIVE",
        "polpermissive": not restrictive,
        "roles": ["public"],
        "cmd": command,
        "qual": qual,
        "with_check": with_check,
    }


def _safe_policies() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for table in PROTECTED_TABLES:
        if table in TENANT_TABLES:
            for command, name in COMMAND_POLICIES.items():
                rows.append(
                    _policy_row(
                        table,
                        PERMIT_POLICIES[command],
                        restrictive=False,
                        command=command,
                    )
                )
                rows.append(
                    _policy_row(
                        table,
                        name,
                        restrictive=True,
                        command=command,
                    )
                )
        else:
            rows.append(_policy_row(table, "memorymaster_team_deny", restrictive=True))
    return rows


@dataclass
class CatalogState:
    current_user: str = "memorymaster_app"
    session_user: str = "memorymaster_app"
    rolsuper: bool = False
    rolbypassrls: bool = False
    rolreplication: bool = False
    rolcreaterole: bool = False
    rolcreatedb: bool = False
    member_of_privileged_role: bool = False
    public_schema_create: bool = False
    tables: dict[str, dict[str, object]] = field(
        default_factory=lambda: {table: _table_row(table) for table in PROTECTED_TABLES}
    )
    policies: list[dict[str, object]] = field(default_factory=_safe_policies)
    metadata_tables: dict[str, dict[str, object]] = field(
        default_factory=lambda: {
            table: _metadata_table_row(table)
            for table in ("cache_meta", "schema_versions")
        }
    )
    claim_identity_indexes: list[dict[str, object]] = field(
        default_factory=_claim_identity_index_rows
    )
    event_head_function: dict[str, object] = field(
        default_factory=_event_head_function_row
    )
    owner_constraint: dict[str, object] = field(
        default_factory=_owner_constraint_row
    )
    event_triggers: list[dict[str, object]] = field(
        default_factory=_event_trigger_rows
    )
    event_guard: dict[str, object] = field(default_factory=_event_guard_row)
    supersession_trigger: dict[str, object] = field(
        default_factory=_supersession_trigger_row
    )
    supersession_guard: dict[str, object] = field(
        default_factory=_supersession_guard_row
    )
    policy_manifest_comment: str | None = None
    schema_v0011_checksum: str = field(default_factory=_v0011_checksum)
    schema_v0012_checksum: str = field(default_factory=_v0012_checksum)
    preconfigured_settings: dict[str, str] = field(default_factory=dict)
    fail_binding_key: str | None = None
    catalog_error_on: str | None = None

    def __post_init__(self) -> None:
        if self.policy_manifest_comment is None:
            self.policy_manifest_comment = _policy_manifest_comment(self.policies)


def _normalize_sql(sql: str) -> str:
    return " ".join(sql.lower().split())


def _config_key(sql: str, params: tuple[object, ...]) -> str | None:
    for key in AUTHORITY_GUCS:
        if key in sql:
            return key
    return next(
        (str(value) for value in params if str(value) in AUTHORITY_GUCS),
        None,
    )


def _config_value(key: str, params: tuple[object, ...]) -> str:
    values = [str(value) for value in params if str(value) != key]
    if not values:
        raise AssertionError(f"set_config for {key} did not bind a value")
    return values[-1]


def _requested_tables(params: tuple[object, ...]) -> set[str]:
    for value in params:
        if isinstance(value, (list, tuple, set, frozenset)):
            return {str(item) for item in value}
    return set()


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self._rows: list[dict[str, object]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        bound = tuple(params)
        self.executed.append((sql, bound))
        normalized = _normalize_sql(sql)
        key = _config_key(sql, bound) if "set_config" in normalized else None
        if key is not None:
            if self.connection.state.fail_binding_key == key:
                raise RuntimeError(f"binding failed for {key}")
            if not re.search(r"\btrue\b", normalized):
                raise AssertionError(f"{key} must be transaction-local")
            self.connection.local_settings[key] = _config_value(key, bound)
            self._rows = [{"set_config": self.connection.local_settings[key]}]
            return

        error_on = self.connection.state.catalog_error_on
        if error_on is not None and error_on in normalized:
            raise RuntimeError("catalog inspection failed")
        if "current_setting" in normalized and "set_config" not in normalized:
            self._rows = [
                {
                    "tenant_id": self.connection.local_settings.get(
                        "memorymaster.tenant_id",
                        self.connection.state.preconfigured_settings.get(
                            "memorymaster.tenant_id", ""
                        ),
                    ),
                    "principal": self.connection.local_settings.get(
                        "memorymaster.principal",
                        self.connection.state.preconfigured_settings.get(
                            "memorymaster.principal", ""
                        ),
                    ),
                    "allowed_scopes": self.connection.local_settings.get(
                        "memorymaster.allowed_scopes",
                        self.connection.state.preconfigured_settings.get(
                            "memorymaster.allowed_scopes", ""
                        ),
                    ),
                }
            ]
        elif "pg_constraint" in normalized:
            self._rows = [dict(self.connection.state.owner_constraint)]
        elif "pg_trigger" in normalized:
            if "tbl.relname = 'claims'" in normalized:
                self._rows = [dict(self.connection.state.supersession_trigger)]
            else:
                self._rows = [dict(row) for row in self.connection.state.event_triggers]
        elif "pg_index" in normalized:
            self._rows = list(self.connection.state.claim_identity_indexes)
        elif "schema_versions" in normalized and "checksum" in normalized:
            self._rows = [
                {
                    "version": 11,
                    "checksum": self.connection.state.schema_v0011_checksum,
                },
                {
                    "version": 12,
                    "checksum": self.connection.state.schema_v0012_checksum,
                },
            ]
        elif "obj_description" in normalized or "manifest_comment" in normalized:
            self._rows = [
                {
                    "manifest_comment": self.connection.state.policy_manifest_comment,
                    "policy_comment": self.connection.state.policy_manifest_comment,
                    "comment": self.connection.state.policy_manifest_comment,
                }
            ]
        elif "pg_proc" in normalized:
            if "memorymaster_claim_supersession_guard" in {
                str(value) for value in bound
            }:
                self._rows = [dict(self.connection.state.supersession_guard)]
            elif "memorymaster_events_append_only_guard" in normalized:
                self._rows = [dict(self.connection.state.event_guard)]
            else:
                self._rows = [dict(self.connection.state.event_head_function)]
        elif "pg_roles" in normalized:
            self._rows = [self._role_row()]
        elif "pg_class" in normalized:
            all_tables = {
                **self.connection.state.tables,
                **self.connection.state.metadata_tables,
            }
            requested = _requested_tables(bound)
            selected = requested or set(self.connection.state.tables)
            self._rows = [all_tables[name] for name in selected if name in all_tables]
        elif "pg_policies" in normalized or "pg_policy" in normalized:
            self._rows = list(self.connection.state.policies)
        elif "has_schema_privilege" in normalized or "pg_namespace" in normalized:
            self._rows = [
                {
                    "public_schema_create": self.connection.state.public_schema_create,
                    "can_create_public": self.connection.state.public_schema_create,
                }
            ]
        elif "current_user" in normalized or "session_user" in normalized:
            self._rows = [self._role_row()]
        elif any(f"from {table}" in normalized for table in PROTECTED_TABLES):
            if not self.connection.local_settings.get("memorymaster.tenant_id"):
                raise PermissionError("protected query has no tenant context")
            self._rows = []
        else:
            self._rows = []

    def _role_row(self) -> dict[str, object]:
        state = self.connection.state
        return {
            "current_user": state.current_user,
            "session_user": state.session_user,
            "rolname": state.current_user,
            "rolsuper": state.rolsuper,
            "rolbypassrls": state.rolbypassrls,
            "rolreplication": state.rolreplication,
            "rolcreaterole": state.rolcreaterole,
            "rolcreatedb": state.rolcreatedb,
            "member_of_privileged_role": state.member_of_privileged_role,
        }

    def fetchone(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, object]]:
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class FakeConnection:
    def __init__(self, state: CatalogState | None = None) -> None:
        self.state = state or CatalogState()
        self.autocommit = True
        self.closed = False
        self.commit_count = 0
        self.rollback_count = 0
        self.local_settings: dict[str, str] = {}
        self.cursor_instance = FakeCursor(self)

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commit_count += 1
        self.local_settings.clear()

    def rollback(self) -> None:
        self.rollback_count += 1
        self.local_settings.clear()

    def close(self) -> None:
        self.closed = True


class FakePsycopg:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.calls = 0

    def connect(self, *_args: object, **_kwargs: object) -> FakeConnection:
        self.calls += 1
        return self.connection


def _team_store(
    *,
    tenant_id: str | None = "tenant-alpha",
    principal: str | None = "agent@example.test",
    allowed_scopes: Sequence[str] = ("project:alpha", "global"),
) -> PostgresStore:
    return PostgresStore(
        "postgresql://db.invalid/app",
        tenant_id=tenant_id,
        require_tenant=True,
        principal=principal,
        allowed_scopes=allowed_scopes,
    )


def _attach_driver(
    store: PostgresStore,
    state: CatalogState | None = None,
) -> tuple[FakeConnection, FakePsycopg]:
    connection = FakeConnection(state)
    driver = FakePsycopg(connection)
    store._psycopg = (driver, object(), object())
    return connection, driver


@pytest.mark.parametrize(
    ("tenant_id", "principal", "allowed_scopes", "match"),
    [
        (None, "agent@example.test", ("project:alpha",), "tenant"),
        ("tenant-alpha", None, ("project:alpha",), "principal"),
        ("tenant-alpha", "   ", ("project:alpha",), "principal"),
        ("tenant-alpha", "agent@example.test", (), "scope"),
        ("tenant-alpha", "agent@example.test", ("*",), "wildcard"),
        ("tenant-alpha", "agent@example.test", ("project:*",), "wildcard"),
        (
            "tenant-alpha",
            "agent@example.test",
            ("project:alpha", "*"),
            "wildcard",
        ),
    ],
)
def test_invalid_team_authority_fails_before_loading_driver(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: str | None,
    principal: str | None,
    allowed_scopes: tuple[str, ...],
    match: str,
) -> None:
    monkeypatch.setattr(
        PostgresStore,
        "_load_psycopg",
        lambda _self: pytest.fail("invalid authority reached the Postgres driver"),
    )

    with pytest.raises(PermissionError, match=match):
        _team_store(
            tenant_id=tenant_id,
            principal=principal,
            allowed_scopes=allowed_scopes,
        ).connect()


def test_ordinary_postgres_runtime_is_rejected_before_loading_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PostgresStore("postgresql://db.invalid/app", require_tenant=False)
    monkeypatch.setattr(
        store,
        "_load_psycopg",
        lambda: pytest.fail("unscoped runtime reached the Postgres driver"),
    )

    with pytest.raises(PermissionError, match="(?i)(team|tenant|scoped|runtime)"):
        store.connect()


def test_team_store_snapshots_allowed_scopes_as_an_immutable_set() -> None:
    scopes = ["project:alpha"]

    store = _team_store(allowed_scopes=scopes)
    scopes.append("project:expanded-after-construction")

    assert store.allowed_scopes == frozenset({"project:alpha"})


def test_connect_binds_all_authority_transaction_locally_and_validates_catalog() -> None:
    store = _team_store()
    connection, driver = _attach_driver(store)

    returned = store.connect()

    assert returned is connection
    assert driver.calls == 1
    assert connection.autocommit is False
    assert connection.commit_count == 0
    set_config_calls = [
        (sql, params)
        for sql, params in connection.cursor_instance.executed
        if "set_config" in sql.lower()
    ]
    assert {_config_key(sql, params) for sql, params in set_config_calls} == set(
        AUTHORITY_GUCS
    )
    for sql, params in set_config_calls:
        assert re.search(r"\btrue\b", _normalize_sql(sql))
        assert "%s" in sql
        assert "tenant-alpha" not in sql
        assert "agent@example.test" not in sql
        assert "project:alpha" not in sql
        assert params
    assert connection.local_settings["memorymaster.tenant_id"] == "tenant-alpha"
    assert connection.local_settings["memorymaster.principal"] == "agent@example.test"
    assert set(
        json.loads(connection.local_settings["memorymaster.allowed_scopes"])
    ) == {"project:alpha", "global"}
    emitted = "\n".join(sql.lower() for sql, _ in connection.cursor_instance.executed)
    assert "pg_roles" in emitted
    assert "pg_class" in emitted
    assert "pg_policies" in emitted
    assert "has_schema_privilege" in emitted or "pg_namespace" in emitted
    role_query = next(
        _normalize_sql(sql)
        for sql, _ in connection.cursor_instance.executed
        if "from pg_roles" in _normalize_sql(sql)
    )
    assert "pg_has_role" in role_query
    assert "'set'" in role_query
    assert "'member'" not in role_query


def test_binding_failure_rolls_back_and_closes_connection() -> None:
    state = CatalogState(fail_binding_key="memorymaster.principal")
    store = _team_store()
    connection, _ = _attach_driver(store, state)

    with pytest.raises(RuntimeError, match="binding failed"):
        store.connect()

    assert connection.rollback_count == 1
    assert connection.closed is True


def test_catalog_query_failure_rolls_back_and_closes_connection() -> None:
    state = CatalogState(catalog_error_on="pg_roles")
    store = _team_store()
    connection, _ = _attach_driver(store, state)

    with pytest.raises(RuntimeError, match="catalog inspection failed"):
        store.connect()

    assert connection.rollback_count == 1
    assert connection.closed is True


@pytest.mark.parametrize("key", AUTHORITY_GUCS)
def test_connect_rejects_preconfigured_authority_guc_defaults(key: str) -> None:
    state = CatalogState(preconfigured_settings={key: "operator-default"})
    store = _team_store()
    connection, _ = _attach_driver(store, state)

    with pytest.raises(PermissionError, match="(?i)(authority|setting|guc|default)"):
        store.connect()

    assert connection.rollback_count == 1
    assert connection.closed is True


def test_catalog_queries_check_set_membership_and_all_privilege_bypasses() -> None:
    connection = FakeConnection()
    cursor = connection.cursor()

    PostgresStore._validate_runtime_role(cursor)
    PostgresStore._validate_runtime_tables(cursor)

    role_sql = next(
        _normalize_sql(sql) for sql, _ in cursor.executed if "from pg_roles" in _normalize_sql(sql)
    )
    table_sql = next(
        _normalize_sql(sql) for sql, _ in cursor.executed if "from pg_class" in _normalize_sql(sql)
    )
    assert "pg_has_role" in role_sql and "'set'" in role_sql
    for attribute in {"rolreplication", "rolcreaterole", "rolcreatedb"}:
        assert attribute in role_sql
    assert "'trigger'" in table_sql


@pytest.mark.parametrize(
    ("case", "match"),
    [
        ("superuser", "superuser"),
        ("bypassrls", "bypassrls"),
        ("replication", "replication"),
        ("createrole", "create.role|createrole"),
        ("createdb", "create.database|createdb"),
        ("session_mismatch", "session"),
        ("privileged_membership", "member|privileged|superuser|bypassrls"),
        ("owner_member", "owner"),
        ("public_create", "create"),
        ("truncate", "truncate"),
        ("references", "references"),
        ("trigger", "trigger"),
    ],
)
def test_connect_rejects_privileged_or_impersonated_runtime_role(
    case: str,
    match: str,
) -> None:
    state = CatalogState()
    if case == "superuser":
        state.rolsuper = True
    elif case == "bypassrls":
        state.rolbypassrls = True
    elif case == "replication":
        state.rolreplication = True
    elif case == "createrole":
        state.rolcreaterole = True
    elif case == "createdb":
        state.rolcreatedb = True
    elif case == "session_mismatch":
        state.session_user = "connection_pooler"
    elif case == "privileged_membership":
        state.member_of_privileged_role = True
    elif case == "owner_member":
        state.tables["claims"]["owner_member"] = True
        state.tables["claims"]["is_owner_member"] = True
    elif case == "public_create":
        state.public_schema_create = True
    elif case == "truncate":
        state.tables["claims"]["can_truncate"] = True
        state.tables["claims"]["has_truncate"] = True
    elif case == "references":
        state.tables["claims"]["can_references"] = True
        state.tables["claims"]["has_references"] = True
    elif case == "trigger":
        state.tables["claims"]["can_trigger"] = True
        state.tables["claims"]["has_trigger"] = True
    store = _team_store()
    connection, _ = _attach_driver(store, state)

    with pytest.raises(PermissionError, match=f"(?i){match}"):
        store.connect()

    assert connection.rollback_count == 1
    assert connection.closed is True


@pytest.mark.parametrize("case", ["missing", "rls_disabled", "rls_not_forced"])
def test_connect_requires_all_fifteen_tables_with_enable_and_force_rls(case: str) -> None:
    state = CatalogState()
    if case == "missing":
        state.tables.pop("rule_stats")
    elif case == "rls_disabled":
        state.tables["claims"]["relrowsecurity"] = False
    elif case == "rls_not_forced":
        state.tables["claims"]["relforcerowsecurity"] = False
    store = _team_store()
    connection, _ = _attach_driver(store, state)

    with pytest.raises(PermissionError, match="(?i)(15|table|row.level|rls|force)"):
        store.connect()

    assert connection.rollback_count == 1
    assert connection.closed is True


def test_team_runtime_store_cannot_initialize_or_migrate_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _team_store()
    monkeypatch.setattr(
        schema_module,
        "load_schema_postgres_sql",
        lambda: pytest.fail("team init read the administrative schema"),
    )
    monkeypatch.setattr(
        store,
        "_load_psycopg",
        lambda: pytest.fail("team init reached the Postgres driver"),
    )

    with pytest.raises(PermissionError, match="(?i)(team|schema|migration|init)"):
        store.init_db()


def test_transaction_local_authority_disappears_after_raw_commit() -> None:
    store = _team_store()
    connection, _ = _attach_driver(store)
    returned = store.connect()

    returned.commit()

    assert connection.local_settings == {}
    with pytest.raises(PermissionError, match="tenant context"):
        with returned.cursor() as cur:
            cur.execute("SELECT id FROM claims")
