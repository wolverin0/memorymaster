"""Fail-closed boundaries for SQLite-only surfaces in Postgres team mode."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from memorymaster.bridges import db_merge, delta_sync
from memorymaster.stores._storage_sources import _SourceItemsMixin
from memorymaster.stores.postgres_store import PostgresStore
from memorymaster.stores.store_factory import create_store


TEAM_ONLY_METHOD_CALLS: tuple[
    tuple[str, tuple[object, ...], dict[str, object]], ...
] = (
    (
        "upsert_external_source",
        (),
        {"source_type": "whatsapp", "display_name": "primary"},
    ),
    (
        "upsert_source_item",
        (),
        {"source_id": 1, "source_item_id": "message-1", "item_type": "text"},
    ),
    ("get_source_item", (), {"source_id": 1, "source_item_id": "message-1"}),
    ("get_source_item_by_id", (1,), {}),
    (
        "add_evidence_item",
        (),
        {"source_item_id": 1, "evidence_type": "transcript", "text": "evidence"},
    ),
    ("list_evidence_items", (), {"source_item_id": 1}),
    (
        "create_action_proposal",
        (),
        {"proposal_type": "task", "title": "Review evidence"},
    ),
    ("get_action_proposal_by_idempotency_key", ("proposal-1",), {}),
    ("update_action_proposal_status", (1,), {"status": "approved"}),
    ("set_source_item_sensitivity", (1, "low"), {}),
    ("set_evidence_item_sensitivity", (1, "low"), {}),
    (
        "enqueue_media_retry",
        (),
        {"source_item_id": 1, "media_key": "media-1", "status": "pending"},
    ),
    ("claim_pending_media_retries", (1,), {}),
    ("record_media_retry_outcome", (1,), {"status": "failed"}),
    ("list_media_retries", (), {"status": "pending"}),
    ("media_retry_status_counts", (), {}),
    ("update_action_proposal_fields", (1,), {"title": "Revised title"}),
    ("list_action_proposals", (), {"status": "candidate"}),
)


@pytest.fixture
def team_store(monkeypatch: pytest.MonkeyPatch) -> PostgresStore:
    store = PostgresStore(
        "postgresql://runtime.invalid/memorymaster",
        tenant_id="tenant-a",
        require_tenant=True,
        principal="agent-a",
        allowed_scopes={"project:alpha"},
    )

    def forbidden_backend_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("team-denied surface attempted Postgres backend access")

    monkeypatch.setattr(store, "_load_psycopg", forbidden_backend_access)
    monkeypatch.setattr(store, "connect", forbidden_backend_access)
    return store


@pytest.mark.parametrize(
    ("method_name", "args", "kwargs"),
    TEAM_ONLY_METHOD_CALLS,
    ids=[entry[0] for entry in TEAM_ONLY_METHOD_CALLS],
)
def test_team_denied_source_surfaces_fail_before_backend_access(
    team_store: PostgresStore,
    method_name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    method = getattr(team_store, method_name)

    with pytest.raises(PermissionError, match="(?i)team"):
        method(*args, **kwargs)


@pytest.mark.parametrize(
    ("method_name", "args", "kwargs"),
    TEAM_ONLY_METHOD_CALLS,
    ids=[f"unbound-{entry[0]}" for entry in TEAM_ONLY_METHOD_CALLS],
)
def test_unbound_postgres_denied_surfaces_cannot_return_early(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    store = PostgresStore("postgresql://runtime.invalid/memorymaster")

    def forbidden_backend_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("unsupported Postgres surface attempted backend access")

    monkeypatch.setattr(store, "_load_psycopg", forbidden_backend_access)
    monkeypatch.setattr(store, "connect", forbidden_backend_access)

    with pytest.raises(PermissionError, match="(?i)(postgres|team)"):
        getattr(store, method_name)(*args, **kwargs)


def test_team_connect_ro_fails_before_driver_or_connection_access(
    team_store: PostgresStore,
) -> None:
    with pytest.raises(PermissionError, match="(?i)(connect_ro|read.only|team)"):
        team_store.connect_ro()


def test_store_factory_normalizes_whitespace_around_postgres_dsn() -> None:
    store = create_store("  postgresql://runtime.invalid/memorymaster\t")

    assert isinstance(store, PostgresStore)
    assert store.dsn == "postgresql://runtime.invalid/memorymaster"


def _forbid_bridge_io(monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Postgres DSN reached bridge filesystem or database access")

    monkeypatch.setattr(module, "Path", forbidden)
    for name in ("connect_ro", "open_conn", "_open_target"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, forbidden)


def _assert_clear_postgres_rejection(action: Callable[[], object]) -> None:
    with pytest.raises((ValueError, PermissionError)) as exc_info:
        action()

    message = str(exc_info.value).lower()
    assert "postgres" in message or "sqlite" in message


@pytest.mark.parametrize(
    ("target_db", "source_db"),
    (
        ("postgresql://runtime.invalid/memorymaster", "source.db"),
        ("target.db", "postgres://runtime.invalid/memorymaster"),
        ("  postgresql://runtime.invalid/memorymaster", "source.db"),
        ("target.db", "\tpostgres://runtime.invalid/memorymaster"),
    ),
    ids=(
        "postgres-target",
        "postgres-source",
        "whitespace-postgres-target",
        "whitespace-postgres-source",
    ),
)
def test_db_merge_rejects_postgres_dsn_before_io(
    monkeypatch: pytest.MonkeyPatch,
    target_db: str,
    source_db: str,
) -> None:
    _forbid_bridge_io(monkeypatch, db_merge)

    _assert_clear_postgres_rejection(
        lambda: db_merge.merge_databases(target_db, source_db)
    )


@pytest.mark.parametrize(
    ("source_db", "output_path"),
    (
        ("postgresql://runtime.invalid/memorymaster", "delta.db"),
        ("source.db", "postgres://runtime.invalid/delta"),
        ("  postgresql://runtime.invalid/memorymaster", "delta.db"),
        ("source.db", "\tpostgres://runtime.invalid/delta"),
    ),
    ids=(
        "postgres-source",
        "postgres-output",
        "whitespace-postgres-source",
        "whitespace-postgres-output",
    ),
)
def test_delta_export_rejects_postgres_dsn_before_io(
    monkeypatch: pytest.MonkeyPatch,
    source_db: str,
    output_path: str,
) -> None:
    _forbid_bridge_io(monkeypatch, delta_sync)

    _assert_clear_postgres_rejection(
        lambda: delta_sync.export_delta(source_db, "", output_path)
    )


def test_cli_rejects_raw_merge_for_tenant_bound_postgres(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from memorymaster.surfaces import cli

    def forbidden_merge(*_args: object, **_kwargs: object) -> None:
        pytest.fail("tenant-bound CLI reached the raw merge bridge")

    monkeypatch.setattr(cli, "MemoryService", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(db_merge, "merge_databases", forbidden_merge)

    exit_code = cli.main(
        [
            "--db",
            "postgresql://runtime.invalid/memorymaster",
            "--tenant",
            "tenant-a",
            "merge-db",
            "--source",
            "source.db",
        ]
    )

    assert exit_code == 2
    output = capsys.readouterr().out.lower()
    assert "merge" in output
    assert "tenant" in output or "team" in output


def test_method_matrix_covers_the_audited_team_denied_surface() -> None:
    expected = {
        name
        for name, member in vars(_SourceItemsMixin).items()
        if callable(member) and not name.startswith("_")
    }

    assert {entry[0] for entry in TEAM_ONLY_METHOD_CALLS} == expected


def test_disposable_postgres_contract_requires_both_roles_and_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import conftest

    require_contract = getattr(conftest, "_require_disposable_postgres_runtime")
    for name in (
        "MEMORYMASTER_TEST_POSTGRES_DSN",
        "MEMORYMASTER_TEST_POSTGRES_APP_DSN",
        "MEMORYMASTER_TEST_POSTGRES_RLS_DISPOSABLE",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("MEMORYMASTER_TEST_POSTGRES_DSN", "postgresql://admin/db")
    with pytest.raises(pytest.skip.Exception):
        require_contract()

    monkeypatch.setenv("MEMORYMASTER_TEST_POSTGRES_APP_DSN", "postgresql://app/db")
    with pytest.raises(pytest.skip.Exception):
        require_contract()

    monkeypatch.setenv("MEMORYMASTER_TEST_POSTGRES_RLS_DISPOSABLE", "1")
    config = require_contract()
    assert config.admin_dsn == "postgresql://admin/db"
    assert config.app_dsn == "postgresql://app/db"


def test_fresh_postgres_service_uses_bound_app_role_without_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import conftest
    import memorymaster.core.service as service_module
    import memorymaster.stores.postgres_store as postgres_module

    calls: list[tuple[str, object]] = []
    config = SimpleNamespace(
        admin_dsn="postgresql://admin/db",
        app_dsn="postgresql://app/db",
    )

    class FakeAdminStore:
        def __init__(self, dsn: str) -> None:
            calls.append(("admin-dsn", dsn))

        def init_db(self) -> None:
            calls.append(("admin-init", True))

    class FakeService:
        def __init__(self, db_target: str, **kwargs: object) -> None:
            calls.append(("app-dsn", db_target))
            calls.append(("app-authority", kwargs))

        def init_db(self) -> None:
            pytest.fail("team runtime attempted schema initialization")

    monkeypatch.setattr(
        conftest,
        "_require_disposable_postgres_runtime",
        lambda: config,
    )
    monkeypatch.setattr(
        conftest,
        "_initialize_disposable_postgres",
        lambda admin_dsn, _app_dsn: FakeAdminStore(admin_dsn).init_db(),
        raising=False,
    )
    monkeypatch.setattr(postgres_module, "PostgresStore", FakeAdminStore)
    monkeypatch.setattr(service_module, "MemoryService", FakeService)

    service = conftest._fresh_postgres_service()

    assert isinstance(service, FakeService)
    assert ("admin-dsn", config.admin_dsn) in calls
    assert ("admin-init", True) in calls
    assert ("app-dsn", config.app_dsn) in calls
    authority = dict(next(value for name, value in calls if name == "app-authority"))
    assert authority["require_tenant"] is True
    assert authority["tenant_id"]
    assert authority["principal"]
    assert authority["allowed_scopes"] == ("project",)
