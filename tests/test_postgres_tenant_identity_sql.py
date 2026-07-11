"""Hermetic PostgreSQL SQL checks for tenant-local identity operations."""
from __future__ import annotations

import inspect

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.stores.postgres_store import PostgresStore


class RecordingCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: object = ()) -> None:
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return None

    def fetchall(self) -> list[object]:
        return []


class RecordingConnection:
    def __init__(self) -> None:
        self.cursor_instance = RecordingCursor()

    def __enter__(self) -> "RecordingConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> RecordingCursor:
        return self.cursor_instance


def _store(monkeypatch) -> tuple[PostgresStore, RecordingConnection]:
    store = PostgresStore(
        "postgresql://db.invalid/app",
        tenant_id="tenant-a",
        require_tenant=True,
    )
    conn = RecordingConnection()
    monkeypatch.setattr(store, "connect", lambda: conn)
    return store, conn


def test_postgres_identity_reads_bind_the_store_tenant(monkeypatch) -> None:
    store, conn = _store(monkeypatch)

    assert store.get_claim_by_idempotency_key("same-key") is None
    assert store.get_claim_by_human_id("mm-abcd") is None
    assert store.find_confirmed_by_tuple(
        subject="subject",
        predicate="uses",
        scope="project:shared",
    ) == []

    emitted = "\n".join(sql for sql, _ in conn.cursor_instance.executed)
    assert emitted.count("tenant_id IS NOT DISTINCT FROM %s") == 3
    assert all(
        params and params[-1] == "tenant-a"
        for _, params in conn.cursor_instance.executed
    )


def test_postgres_bound_tenant_cannot_be_overridden(monkeypatch) -> None:
    store, conn = _store(monkeypatch)

    with pytest.raises(PermissionError, match="bound tenant"):
        store.get_claim_by_idempotency_key("same-key", tenant_id="tenant-b")
    with pytest.raises(PermissionError, match="bound tenant"):
        store.create_claim(
            "cross-tenant write",
            [CitationInput(source="test")],
            tenant_id="tenant-b",
        )

    assert conn.cursor_instance.executed == []


def test_postgres_insert_conflict_fallback_is_tenant_qualified() -> None:
    source = inspect.getsource(PostgresStore.create_claim)

    assert "ON CONFLICT DO NOTHING" in source
    assert "ON CONFLICT (idempotency_key)" not in source
    assert "tenant_id IS NOT DISTINCT FROM %s" in source
    assert "normalized_tenant_id" in source
