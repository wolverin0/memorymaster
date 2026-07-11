from __future__ import annotations

import pytest

import memorymaster.core.service as service_module
from memorymaster.core.service import MemoryService
from memorymaster.stores.postgres_store import PostgresStore
from memorymaster.stores.store_factory import create_store


class FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        self.executed.append((sql, params))


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakeCursor()
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    def close(self) -> None:
        self.closed = True


class FakePsycopg:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.calls = 0

    def connect(self, *_args, **_kwargs) -> FakeConnection:
        self.calls += 1
        return self.connection


def test_required_tenant_fails_before_loading_postgres_driver(monkeypatch) -> None:
    store = PostgresStore("postgresql://db.invalid/app", require_tenant=True)
    monkeypatch.setattr(
        store,
        "_load_psycopg",
        lambda: pytest.fail("missing tenant reached the Postgres driver"),
    )

    with pytest.raises(PermissionError, match="tenant"):
        store.connect()


def test_non_team_postgres_runtime_fails_closed_before_driver_load(
    monkeypatch,
) -> None:
    store = PostgresStore(
        "postgresql://db.invalid/app",
        tenant_id="tenant-alpha",
    )
    monkeypatch.setattr(
        store,
        "_load_psycopg",
        lambda: pytest.fail("non-team Postgres runtime reached the driver"),
    )

    with pytest.raises(PermissionError, match="(?i)(team|authority|sqlite)"):
        store.connect()


def test_store_factory_propagates_postgres_tenant_context() -> None:
    store = create_store(
        "postgresql://db.invalid/app",
        tenant_id="tenant-alpha",
        require_tenant=True,
        principal="agent@example.test",
        allowed_scopes=("project:alpha",),
    )

    assert isinstance(store, PostgresStore)
    assert store.tenant_id == "tenant-alpha"
    assert store.require_tenant is True


def test_memory_service_propagates_tenant_requirement(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_store(db_target, **kwargs):
        captured.update({"db_target": db_target, **kwargs})
        return object()

    monkeypatch.setattr(service_module, "create_store", fake_create_store)
    monkeypatch.setattr(MemoryService, "_init_qdrant", staticmethod(lambda: None))

    MemoryService(
        "postgresql://db.invalid/app",
        tenant_id="tenant-alpha",
        require_tenant=True,
        principal="agent@example.test",
        allowed_scopes=("project:alpha",),
    )

    assert captured["tenant_id"] == "tenant-alpha"
    assert captured["require_tenant"] is True
