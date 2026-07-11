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


def test_connect_sets_tenant_context_on_every_connection() -> None:
    connection = FakeConnection()
    driver = FakePsycopg(connection)
    store = PostgresStore(
        "postgresql://db.invalid/app",
        tenant_id="tenant-alpha",
        require_tenant=True,
    )
    store._psycopg = (driver, object(), object())

    returned = store.connect()

    assert returned is connection
    assert driver.calls == 1
    sql, params = connection.cursor_instance.executed[0]
    assert "set_config('memorymaster.tenant_id'" in sql
    assert params == ("tenant-alpha",)


def test_store_factory_propagates_postgres_tenant_context() -> None:
    store = create_store(
        "postgresql://db.invalid/app",
        tenant_id="tenant-alpha",
        require_tenant=True,
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
    )

    assert captured["tenant_id"] == "tenant-alpha"
    assert captured["require_tenant"] is True
