"""Adversarial PostgreSQL supersession boundary and atomicity contracts."""
from __future__ import annotations

import copy
import importlib
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Sequence

import pytest

from memorymaster.stores.postgres_store import PostgresStore


GUARD_NAME = "memorymaster_claim_supersession_guard"
TRIGGER_NAME = "trg_claims_supersession_boundary"
GUARD_SOURCE = """
DECLARE
    reference_id BIGINT;
BEGIN
    FOREACH reference_id IN ARRAY ARRAY[
        NEW.supersedes_claim_id,
        NEW.replaced_by_claim_id
    ] LOOP
        IF reference_id IS NOT NULL AND (
            reference_id = NEW.id
            OR NOT EXISTS (
                SELECT 1
                FROM public.claims AS referenced
                WHERE referenced.id = reference_id
                  AND referenced.tenant_id IS NOT DISTINCT FROM NEW.tenant_id
                  AND referenced.scope = NEW.scope
                  AND referenced.visibility IS NOT DISTINCT FROM NEW.visibility
                  AND referenced.source_agent IS NOT DISTINCT FROM NEW.source_agent
            )
        ) THEN
            RAISE EXCEPTION 'supersession reference is outside the authorized boundary'
                USING ERRCODE = '42501';
        END IF;
    END LOOP;
    RETURN NEW;
END;
""".strip()


def _safe_trigger() -> dict[str, object]:
    return {
        "trigger_name": TRIGGER_NAME,
        "table_schema": "public",
        "table_name": "claims",
        "enabled_code": "O",
        "is_internal": False,
        "function_schema": "public",
        "function_name": GUARD_NAME,
        "trigger_definition": (
            f"CREATE TRIGGER {TRIGGER_NAME} BEFORE INSERT OR UPDATE OF "
            "tenant_id, scope, visibility, source_agent, supersedes_claim_id, "
            "replaced_by_claim_id ON public.claims "
            f"FOR EACH ROW EXECUTE FUNCTION public.{GUARD_NAME}()"
        ),
    }


def _safe_guard() -> dict[str, object]:
    return {
        "schema_name": "public",
        "function_name": GUARD_NAME,
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
        "function_source": GUARD_SOURCE,
    }


class GuardCursor:
    def __init__(
        self,
        trigger: dict[str, object] | None,
        guard: dict[str, object] | None,
    ) -> None:
        self.trigger = copy.deepcopy(trigger)
        self.guard = copy.deepcopy(guard)
        self.rows: list[dict[str, object]] = []

    def execute(self, sql: str, _params: Sequence[object] = ()) -> None:
        normalized = " ".join(sql.lower().split())
        if "pg_trigger" in normalized:
            self.rows = [] if self.trigger is None else [dict(self.trigger)]
        elif "pg_proc" in normalized:
            self.rows = [] if self.guard is None else [dict(self.guard)]
        else:
            raise AssertionError(f"unexpected guard catalog SQL: {normalized}")

    def fetchone(self) -> dict[str, object] | None:
        return dict(self.rows[0]) if self.rows else None

    def fetchall(self) -> list[dict[str, object]]:
        return [dict(row) for row in self.rows]


class SupersessionTriggerInventoryCursor(GuardCursor):
    def __init__(self) -> None:
        super().__init__(_safe_trigger(), _safe_guard())
        self.triggers = [
            _safe_trigger(),
            {
                **_safe_trigger(),
                "trigger_name": "trg_claims_supersession_exfiltration",
            },
        ]

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        normalized = " ".join(sql.lower().split())
        if "pg_trigger" in normalized:
            rows = list(self.triggers)
            if "tg.tgname = %s" in normalized and params:
                rows = [row for row in rows if row["trigger_name"] == params[0]]
            self.rows = [dict(row) for row in rows]
            return
        super().execute(sql, params)


def _validator():
    validator = getattr(PostgresStore, "_validate_claim_supersession_guard", None)
    assert callable(validator), "team startup must validate the supersession guard"
    return validator


def test_runtime_accepts_only_exact_supersession_guard_catalog() -> None:
    _validator()(GuardCursor(_safe_trigger(), _safe_guard()))


def test_runtime_rejects_extra_claims_trigger_outside_exact_catalog() -> None:
    with pytest.raises(PermissionError, match="(?i)(supersession|guard|trigger|catalog)"):
        _validator()(SupersessionTriggerInventoryCursor())


@pytest.mark.parametrize(
    "case",
    ["missing", "scope", "visibility", "owner", "boundary-update", "disabled"],
)
def test_runtime_rejects_supersession_guard_drift(case: str) -> None:
    trigger = _safe_trigger()
    guard = _safe_guard()
    if case == "missing":
        trigger = None
    elif case == "scope":
        guard["function_source"] = GUARD_SOURCE.replace(
            "AND referenced.scope = NEW.scope\n",
            "",
        )
    elif case == "visibility":
        guard["function_source"] = GUARD_SOURCE.replace(
            "AND referenced.visibility IS NOT DISTINCT FROM NEW.visibility\n",
            "",
        )
    elif case == "owner":
        guard["function_source"] = GUARD_SOURCE.replace(
            "AND referenced.source_agent IS NOT DISTINCT FROM NEW.source_agent\n",
            "",
        )
    elif case == "boundary-update":
        trigger["trigger_definition"] = str(trigger["trigger_definition"]).replace(
            "tenant_id, scope, visibility, source_agent, ",
            "",
        )
    elif case == "disabled":
        trigger["enabled_code"] = "D"

    with pytest.raises(PermissionError, match="(?i)(supersession|guard|trigger|boundary)"):
        _validator()(GuardCursor(trigger, guard))


def test_v0012_installs_nonleaking_complete_boundary_guard() -> None:
    migration = importlib.import_module(
        "memorymaster.stores.migrations.0012_principal_local_claim_identities"
    )
    function = str(getattr(migration, "_SUPERSESSION_GUARD_FUNCTION", ""))
    trigger = str(getattr(migration, "_SUPERSESSION_GUARD_TRIGGER", ""))
    canonical = " ".join((function + " " + trigger).lower().split())

    for token in (
        "tenant_id",
        "scope",
        "visibility",
        "source_agent",
        "supersedes_claim_id",
        "replaced_by_claim_id",
        "42501",
    ):
        assert token in canonical
    assert "outside the authorized boundary" in canonical
    assert "does not exist" not in canonical
    assert (
        "update of tenant_id, scope, visibility, source_agent, "
        "supersedes_claim_id, replaced_by_claim_id"
    ) in canonical


class PreflightCursor:
    def __init__(self, invalid_edges: int) -> None:
        self.invalid_edges = invalid_edges
        self.executed: list[str] = []
        self.row: dict[str, int] | None = None

    def __enter__(self) -> PreflightCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, _params: Sequence[object] = ()) -> None:
        canonical = " ".join(sql.lower().split())
        self.executed.append(canonical)
        if "invalid_supersession_edges" in canonical:
            self.row = {"invalid_supersession_edges": self.invalid_edges}

    def fetchone(self) -> dict[str, int] | None:
        return self.row


class PreflightConnection:
    def __init__(self, invalid_edges: int) -> None:
        self.cursor_instance = PreflightCursor(invalid_edges)
        self.commits = 0

    def cursor(self) -> PreflightCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1


def test_v0012_rejects_legacy_invalid_supersession_edges_before_ddl() -> None:
    migration = importlib.import_module(
        "memorymaster.stores.migrations.0012_principal_local_claim_identities"
    )
    sql = " ".join(
        str(getattr(migration, "POSTGRES_SUPERSESSION_PREFLIGHT_SQL", ""))
        .lower()
        .split()
    )
    for token in (
        "invalid_supersession_edges",
        "supersedes_claim_id",
        "replaced_by_claim_id",
        "tenant_id",
        "scope",
        "visibility",
        "source_agent",
        "is distinct from",
    ):
        assert token in sql

    conn = PreflightConnection(invalid_edges=2)
    with pytest.raises(RuntimeError, match="2 invalid supersession"):
        migration.apply_postgres(conn)

    assert conn.commits == 0
    assert len(conn.cursor_instance.executed) == 1


class AtomicCursor:
    def __init__(self, connection: AtomicConnection) -> None:
        self.connection = connection
        self.rows: list[dict[str, object]] = []
        self.rowcount = 0

    def __enter__(self) -> AtomicCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        normalized = " ".join(sql.lower().split())
        self.rowcount = 0
        if normalized.startswith("select") and "from claims" in normalized:
            ids = {int(value) for value in params if isinstance(value, int)}
            self.rows = [
                dict(row)
                for claim_id, row in self.connection.state["claims"].items()
                if claim_id in ids
            ]
            return
        if "set status = 'superseded'" in normalized:
            self.connection.state["claims"][1]["status"] = "superseded"
            self.connection.state["claims"][1]["replaced_by_claim_id"] = 2
            self.rowcount = 1
            return
        if "set supersedes_claim_id" in normalized:
            raise RuntimeError("injected replacement update failure")
        raise AssertionError(f"unexpected atomic supersession SQL: {normalized}")

    def fetchone(self) -> dict[str, object] | None:
        return dict(self.rows[0]) if self.rows else None

    def fetchall(self) -> list[dict[str, object]]:
        return [dict(row) for row in self.rows]


class AtomicConnection:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state
        self.snapshot: dict[str, object] | None = None
        self.enter_count = 0
        self.cursor_instance = AtomicCursor(self)

    def __enter__(self) -> AtomicConnection:
        self.enter_count += 1
        self.snapshot = copy.deepcopy(self.state)
        return self

    def __exit__(self, exc_type, *_args: object) -> None:
        if exc_type is not None and self.snapshot is not None:
            self.state.clear()
            self.state.update(self.snapshot)
        return None

    def cursor(self) -> AtomicCursor:
        return self.cursor_instance


class EventFailureCursor(AtomicCursor):
    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        normalized = " ".join(sql.lower().split())
        if "set supersedes_claim_id" in normalized:
            self.connection.state["claims"][2]["supersedes_claim_id"] = 1
            self.rowcount = 1
            return
        super().execute(sql, params)


class EventFailureConnection(AtomicConnection):
    def __init__(self, state: dict[str, object]) -> None:
        super().__init__(state)
        self.cursor_instance = EventFailureCursor(self)


def test_replacement_update_failure_rolls_back_entire_supersession(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, object] = {
        "claims": {
            1: {
                "id": 1,
                "status": "confirmed",
                "version": 4,
                "replaced_by_claim_id": None,
                "supersedes_claim_id": None,
            },
            2: {
                "id": 2,
                "status": "candidate",
                "version": 2,
                "replaced_by_claim_id": None,
                "supersedes_claim_id": None,
            },
        },
        "events": [],
    }
    before = copy.deepcopy(state)
    connection = AtomicConnection(state)
    store = PostgresStore(
        "postgresql://runtime.invalid/memorymaster",
        tenant_id="tenant-a",
        require_tenant=True,
        principal="alice",
        allowed_scopes={"project:a"},
    )
    monkeypatch.setattr(store, "connect", lambda: connection)
    monkeypatch.setattr(
        store,
        "get_claim",
        lambda *_args, **_kwargs: SimpleNamespace(
            id=1,
            status="confirmed",
            replaced_by_claim_id=None,
        ),
    )

    def legacy_first_write(*_args, **_kwargs):
        state["claims"][1]["status"] = "superseded"  # type: ignore[index]
        state["claims"][1]["replaced_by_claim_id"] = 2  # type: ignore[index]

    monkeypatch.setattr(store, "apply_status_transition", legacy_first_write)
    monkeypatch.setattr(
        store,
        "set_supersedes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("injected replacement update failure")
        ),
    )
    monkeypatch.setattr(
        store,
        "_insert_event_row",
        lambda *_args, **_kwargs: state["events"].append("event"),  # type: ignore[union-attr]
    )

    with pytest.raises(RuntimeError, match="injected replacement"):
        store.mark_superseded(1, 2, "atomic boundary test")

    assert connection.enter_count == 1
    assert state == before


def test_event_insert_failure_rolls_back_entire_supersession(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, object] = {
        "claims": {
            1: {
                "id": 1,
                "status": "confirmed",
                "version": 4,
                "replaced_by_claim_id": None,
                "supersedes_claim_id": None,
            },
            2: {
                "id": 2,
                "status": "candidate",
                "version": 2,
                "replaced_by_claim_id": None,
                "supersedes_claim_id": None,
            },
        },
        "events": [],
    }
    before = copy.deepcopy(state)
    connection = EventFailureConnection(state)
    store = PostgresStore("postgresql://runtime.invalid/memorymaster")
    monkeypatch.setattr(store, "connect", lambda: connection)
    monkeypatch.setattr(
        store,
        "_insert_event_row",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("injected event insert failure")
        ),
    )

    with pytest.raises(RuntimeError, match="injected event insert"):
        store.mark_superseded(1, 2, "atomic event boundary test")

    assert connection.enter_count == 1
    assert state == before


class NoWriteCursor:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, sql: str, _params: Sequence[object] = ()) -> None:
        self.executed.append(" ".join(sql.split()))


def test_postgres_atomic_supersession_rejects_archived_source() -> None:
    store = PostgresStore("postgresql://runtime.invalid/memorymaster")
    cursor = NoWriteCursor()

    with pytest.raises(ValueError, match="Invalid transition"):
        store._apply_atomic_supersession(
            None,
            cursor,
            {
                "id": 1,
                "status": "archived",
                "version": 3,
                "replaced_by_claim_id": None,
                "supersedes_claim_id": None,
            },
            {
                "id": 2,
                "status": "candidate",
                "version": 1,
                "replaced_by_claim_id": None,
                "supersedes_claim_id": None,
            },
            "invalid archived supersession",
            datetime.now(timezone.utc),
        )

    assert cursor.executed == []
