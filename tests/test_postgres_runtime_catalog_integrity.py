"""Adversarial runtime catalog contracts for PostgreSQL team mode.

These tests deliberately exercise catalog drift that can turn a constrained
application role into a cross-tenant oracle or make the event ledger mutable.
They use catalog doubles only; no PostgreSQL service is required.
"""
from __future__ import annotations

import importlib
import re
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Sequence

import pytest

from memorymaster.stores.postgres_store import (
    POSTGRES_PROTECTED_TABLES,
    PostgresStore,
)


def _v0011_event_head_definition() -> str:
    migration = importlib.import_module(
        "memorymaster.stores.migrations.0011_postgres_scoped_force_rls"
    )
    return str(migration._EVENT_HEAD_FUNCTION)


def _function_source(definition: str) -> str:
    match = re.search(r"\bAS\s+\$\$(.*)\$\$\s*;?\s*$", definition, re.I | re.S)
    assert match is not None
    return match.group(1).strip()


@dataclass(frozen=True)
class EventHeadCatalogState:
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
    function_source: str = ""
    function_definition: str = ""

    def as_row(self) -> dict[str, object]:
        definition = self.function_definition or _v0011_event_head_definition()
        row = vars(self).copy()
        row["function_definition"] = definition
        row["function_source"] = self.function_source or _function_source(definition)
        return row


class SingleRowCursor:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row
        self.statements: list[str] = []

    def execute(self, statement: str, _params: object = None) -> None:
        self.statements.append(" ".join(statement.split()).lower())

    def fetchone(self) -> dict[str, object] | None:
        return deepcopy(self.row)


def test_event_head_catalog_query_reads_exact_function_metadata() -> None:
    cursor = SingleRowCursor(EventHeadCatalogState().as_row())

    PostgresStore._validate_event_chain_head_function(cursor)

    emitted = "\n".join(cursor.statements)
    for token in (
        "pg_get_function_result",
        "pg_language",
        "p.prosrc",
        "p.provolatile",
        "p.proparallel",
        "p.proleakproof",
        "p.proisstrict",
    ):
        assert token in emitted


@pytest.mark.parametrize(
    "change",
    [
        {"result_signature": "TABLE(global_event_hash text, tenant_event_hash bigint)"},
        {"language_name": "sql"},
        {
            "function_config": (
                "search_path=pg_catalog, pg_temp",
                "statement_timeout=0",
            )
        },
        {"volatility": "i"},
        {"parallel_safety": "s"},
        {"leakproof": True},
        {"strict": True},
        {"owner_member": True},
        {"owner_superuser": False, "owner_bypassrls": False},
    ],
)
def test_event_head_rejects_exact_metadata_drift(change: dict[str, object]) -> None:
    state = replace(EventHeadCatalogState(), **change)
    cursor = SingleRowCursor(state.as_row())

    with pytest.raises(PermissionError, match="(?i)(event|function|signature|catalog|unsafe|drift)"):
        PostgresStore._validate_event_chain_head_function(cursor)


def test_event_head_rejects_comment_token_decoy_with_cross_tenant_body() -> None:
    malicious_definition = """
        CREATE FUNCTION public.memorymaster_event_chain_head()
        RETURNS TABLE (global_event_hash text, tenant_event_hash text)
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$
        -- Decoy contract tokens: memorymaster.tenant_id, global_event_hash,
        -- tenant_event_hash, from public.events.
        BEGIN
            RETURN QUERY SELECT event.event_hash, event.tenant_event_hash
            FROM public.events AS event ORDER BY event.id DESC LIMIT 1;
        END;
        $$
    """.strip()
    state = replace(
        EventHeadCatalogState(),
        function_source=_function_source(malicious_definition),
        function_definition=malicious_definition,
    )

    with pytest.raises(PermissionError, match="(?i)(event|function|body|definition|unsafe|drift)"):
        PostgresStore._validate_event_chain_head_function(
            SingleRowCursor(state.as_row())
        )


def _identity_index_rows() -> list[dict[str, object]]:
    return [
        {
            "index_name": name,
            "relname": name,
            "indisunique": True,
            "indisprimary": False,
            "indisvalid": True,
            "indisready": True,
            "indexdef": definition,
            "predicate": predicate,
        }
        for name, (definition, predicate) in sorted(
            PostgresStore._expected_claim_identity_catalog().items()
        )
    ]


def _claims_primary_key_row() -> dict[str, object]:
    return {
        "index_name": "claims_pkey",
        "relname": "claims_pkey",
        "indisunique": True,
        "indisprimary": True,
        "indisvalid": True,
        "indisready": True,
        "indexdef": "CREATE UNIQUE INDEX claims_pkey ON public.claims USING btree (id)",
        "predicate": None,
    }


def _rogue_unique_constraint_row() -> dict[str, object]:
    return {
        "index_name": "rogue_claim_identity_oracle",
        "relname": "rogue_claim_identity_oracle",
        "indisunique": True,
        "indisprimary": False,
        "indisvalid": True,
        "indisready": True,
        "indexdef": (
            "CREATE UNIQUE INDEX rogue_claim_identity_oracle ON public.claims "
            "USING btree (idempotency_key)"
        ),
        "predicate": "idempotency_key IS NOT NULL",
    }


class UniqueIndexCatalogCursor:
    """Apply the SQL's catalog filter so prefix filtering can be exploited."""

    def __init__(self, rows: Sequence[dict[str, object]]) -> None:
        self.all_rows = [dict(row) for row in rows]
        self.rows: list[dict[str, object]] = []
        self.statement = ""

    def execute(self, statement: str, _params: object = None) -> None:
        self.statement = " ".join(statement.lower().split())
        rows = list(self.all_rows)
        if "relname like 'idx_claims_%'" in self.statement:
            rows = [row for row in rows if str(row["index_name"]).startswith("idx_claims_")]
        if "not x.indisprimary" in self.statement or "x.indisprimary = false" in self.statement:
            rows = [row for row in rows if not bool(row.get("indisprimary"))]
        self.rows = rows

    def fetchall(self) -> list[dict[str, object]]:
        return [dict(row) for row in self.rows]


def test_claim_identity_catalog_query_scans_every_nonprimary_unique_index() -> None:
    cursor = UniqueIndexCatalogCursor(_identity_index_rows() + [_claims_primary_key_row()])

    PostgresStore._validate_claim_identity_indexes(cursor)

    assert "relname like" not in cursor.statement
    assert "indisprimary" in cursor.statement


def test_arbitrarily_named_claims_unique_constraint_is_rejected() -> None:
    cursor = UniqueIndexCatalogCursor(
        _identity_index_rows()
        + [_claims_primary_key_row(), _rogue_unique_constraint_row()]
    )

    with pytest.raises(PermissionError, match="(?i)(identity|index|unique|catalog|unsafe)"):
        PostgresStore._validate_claim_identity_indexes(cursor)


def _protected_table_row(table: str) -> dict[str, object]:
    return {
        "table_name": table,
        "relname": table,
        "relrowsecurity": True,
        "relforcerowsecurity": True,
        "owner_member": False,
        "can_truncate": False,
        "can_references": False,
        "can_trigger": False,
        "can_select": table == "events",
        "can_insert": table == "events",
        "can_update": False,
        "can_update_any_column": False,
        "can_delete": False,
    }


class ProtectedTableCursor:
    def __init__(self, rows: Sequence[dict[str, object]]) -> None:
        self.rows = [dict(row) for row in rows]
        self.statement = ""

    def execute(self, statement: str, _params: object = None) -> None:
        self.statement = " ".join(statement.lower().split())

    def fetchall(self) -> list[dict[str, object]]:
        return [dict(row) for row in self.rows]


@pytest.mark.parametrize("privilege", ["update", "delete"])
def test_runtime_role_cannot_mutate_existing_events(privilege: str) -> None:
    rows = [_protected_table_row(table) for table in POSTGRES_PROTECTED_TABLES]
    events = next(row for row in rows if row["table_name"] == "events")
    events[f"can_{privilege}"] = True

    with pytest.raises(PermissionError, match=f"(?i)(events|{privilege}|append|privilege)"):
        PostgresStore._validate_runtime_tables(ProtectedTableCursor(rows))


def test_runtime_role_cannot_hold_column_level_event_update() -> None:
    rows = [_protected_table_row(table) for table in POSTGRES_PROTECTED_TABLES]
    events = next(row for row in rows if row["table_name"] == "events")
    events["can_update_any_column"] = True
    cursor = ProtectedTableCursor(rows)

    with pytest.raises(PermissionError, match="(?i)(events|update|append|privilege)"):
        PostgresStore._validate_runtime_tables(cursor)

    assert "has_any_column_privilege" in cursor.statement


@pytest.mark.parametrize("privilege", ["select", "insert"])
def test_runtime_role_requires_event_read_append_privileges(privilege: str) -> None:
    rows = [_protected_table_row(table) for table in POSTGRES_PROTECTED_TABLES]
    events = next(row for row in rows if row["table_name"] == "events")
    events[f"can_{privilege}"] = False

    with pytest.raises(PermissionError, match=f"(?i)(events|{privilege}|append|privilege)"):
        PostgresStore._validate_runtime_tables(ProtectedTableCursor(rows))


EVENT_GUARD_SOURCE = """
BEGIN
    RAISE EXCEPTION 'events table is append-only; % is not allowed', TG_OP;
END;
""".strip()


def _safe_event_triggers() -> list[dict[str, object]]:
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


def _safe_event_guard() -> dict[str, object]:
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
        "function_source": EVENT_GUARD_SOURCE,
    }


class EventAppendOnlyCatalogCursor:
    def __init__(
        self,
        triggers: Sequence[dict[str, object]],
        guard: dict[str, object] | None,
    ) -> None:
        self.triggers = [dict(row) for row in triggers]
        self.guard = deepcopy(guard)
        self.rows: list[dict[str, object]] = []

    def execute(self, statement: str, params: object = None) -> None:
        normalized = " ".join(statement.lower().split())
        if "pg_trigger" in normalized:
            rows = list(self.triggers)
            if "tg.tgname = any" in normalized and params:
                allowed = set(params[0])
                rows = [row for row in rows if row["trigger_name"] in allowed]
            self.rows = [dict(row) for row in rows]
        elif "pg_proc" in normalized:
            self.rows = [] if self.guard is None else [dict(self.guard)]
        else:
            raise AssertionError(f"unexpected append-only catalog query: {normalized}")

    def fetchone(self) -> dict[str, object] | None:
        return dict(self.rows[0]) if self.rows else None

    def fetchall(self) -> list[dict[str, object]]:
        return [dict(row) for row in self.rows]


def _append_only_validator():
    validator = getattr(PostgresStore, "_validate_event_append_only_catalog", None)
    assert callable(validator), "team runtime must validate the append-only event catalog"
    return validator


def test_runtime_accepts_exact_append_only_event_catalog() -> None:
    cursor = EventAppendOnlyCatalogCursor(_safe_event_triggers(), _safe_event_guard())

    _append_only_validator()(cursor)


@pytest.mark.parametrize(
    "case",
    [
        "missing_update",
        "altered_update",
        "altered_delete",
        "missing_guard",
        "altered_guard",
        "guard_owner_member",
        "extra_trigger",
    ],
)
def test_runtime_rejects_append_only_event_catalog_drift(case: str) -> None:
    triggers = _safe_event_triggers()
    guard = _safe_event_guard()
    if case == "missing_update":
        triggers = [row for row in triggers if not str(row["trigger_name"]).endswith("update")]
    elif case == "altered_update":
        triggers[0]["trigger_definition"] = str(triggers[0]["trigger_definition"]).replace(
            "BEFORE UPDATE", "AFTER UPDATE"
        )
    elif case == "altered_delete":
        triggers[1]["trigger_definition"] = str(triggers[1]["trigger_definition"]).replace(
            "FOR EACH ROW", "FOR EACH STATEMENT"
        )
    elif case == "missing_guard":
        guard = None
    elif case == "altered_guard":
        assert guard is not None
        guard["function_source"] = "BEGIN RETURN NEW; END;"
    elif case == "guard_owner_member":
        assert guard is not None
        guard["owner_member"] = True
    elif case == "extra_trigger":
        rogue = deepcopy(triggers[0])
        rogue["trigger_name"] = "trg_events_payload_exfiltration"
        triggers.append(rogue)

    cursor = EventAppendOnlyCatalogCursor(triggers, guard)
    with pytest.raises(PermissionError, match="(?i)(event|append|trigger|function|catalog|unsafe|drift)"):
        _append_only_validator()(cursor)
