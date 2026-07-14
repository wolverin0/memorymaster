from __future__ import annotations

from dataclasses import dataclass

import pytest

import memorymaster.stores._storage_schema as schema_module
from memorymaster.stores.postgres_store import PostgresStore


def test_init_db_uses_admin_connection_before_reading_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PostgresStore("postgresql://db.invalid/admin")

    def reject_admin_connection():
        raise PermissionError("migration role requires BYPASSRLS")

    monkeypatch.setattr(store, "_connect_schema_admin", reject_admin_connection, raising=False)
    monkeypatch.setattr(
        schema_module,
        "load_schema_postgres_sql",
        lambda: pytest.fail("unverified migration role read the administrative schema"),
    )

    with pytest.raises(PermissionError, match="BYPASSRLS"):
        store.init_db()


@dataclass
class _AdminRole:
    rolsuper: bool = False
    rolbypassrls: bool = False


class _AdminCursor:
    def __init__(self, role: _AdminRole) -> None:
        self.role = role
        self._row: dict[str, object] | None = None

    def __enter__(self) -> _AdminCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, _params: object = None) -> None:
        assert "pg_roles" in sql
        self._row = {
            "current_user": "memorymaster_migrator",
            "session_user": "memorymaster_migrator",
            "rolsuper": self.role.rolsuper,
            "rolbypassrls": self.role.rolbypassrls,
        }

    def fetchone(self) -> dict[str, object] | None:
        return self._row


class _AdminConnection:
    def __init__(self, role: _AdminRole) -> None:
        self.role = role
        self.autocommit = True
        self.closed = False
        self.rollback_count = 0

    def cursor(self) -> _AdminCursor:
        return _AdminCursor(self.role)

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        self.closed = True


class _AdminDriver:
    def __init__(self, connection: _AdminConnection) -> None:
        self.connection = connection

    def connect(self, *_args: object, **_kwargs: object) -> _AdminConnection:
        return self.connection


def _admin_store(role: _AdminRole) -> tuple[PostgresStore, _AdminConnection]:
    store = PostgresStore("postgresql://db.invalid/admin")
    connection = _AdminConnection(role)
    store._psycopg = (_AdminDriver(connection), object(), object())
    return store, connection


def test_schema_admin_connection_rejects_unprivileged_runtime_role() -> None:
    store, connection = _admin_store(_AdminRole())

    with pytest.raises(PermissionError, match="(?i)(migration|bypassrls|superuser)"):
        store._connect_schema_admin()

    assert connection.rollback_count == 1
    assert connection.closed is True


@pytest.mark.parametrize("role", [_AdminRole(rolsuper=True), _AdminRole(rolbypassrls=True)])
def test_schema_admin_connection_accepts_explicit_privileged_role(role: _AdminRole) -> None:
    store, connection = _admin_store(role)

    assert store._connect_schema_admin() is connection
    assert connection.autocommit is False
    assert connection.closed is False


def test_failed_connection_cleanup_closes_even_if_rollback_fails() -> None:
    class BrokenRollbackConnection:
        closed = False

        def rollback(self) -> None:
            raise RuntimeError("rollback failed")

        def close(self) -> None:
            self.closed = True

    connection = BrokenRollbackConnection()

    with pytest.raises(RuntimeError, match="rollback failed"):
        PostgresStore._cleanup_failed_connection(connection)

    assert connection.closed is True
