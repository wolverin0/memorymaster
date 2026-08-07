from __future__ import annotations

import importlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.core.service import MemoryService
from memorymaster.core.session_scope import (
    SessionScopeRepository,
    SessionScopeResolver,
    hash_session_id,
)
from memorymaster.public.v1 import recall, remember


def _service(tmp_path: Path) -> MemoryService:
    service = MemoryService(tmp_path / "scope.db", workspace_root=tmp_path)
    service.init_db()
    return service


def test_migration_creates_session_scope_schema_idempotently(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.init_db()
    with service.store.connect() as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_scope_bindings'"
        ).fetchone()
        versions = conn.execute(
            "SELECT COUNT(*) FROM schema_versions WHERE version=19"
        ).fetchone()[0]
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(session_scope_bindings)")
        }
    assert table is not None
    assert versions == 1
    assert {
        "session_hash",
        "scope",
        "binding_source",
        "expires_at",
        "ended_at",
    } <= columns


def test_sqlite_only_migration_fails_closed_for_postgres() -> None:
    migration = importlib.import_module(
        "memorymaster.stores.migrations.0019_session_scope_bindings"
    )
    with pytest.raises(RuntimeError, match="SQLite-only"):
        migration.apply_postgres(object())


def test_repository_hashes_session_identity_and_preserves_history(tmp_path: Path) -> None:
    service = _service(tmp_path)
    repository = SessionScopeRepository(service.store.db_path)
    first = repository.bind(
        "raw-session-123",
        scope="project:alpha",
        source_agent="hermes-vm",
        platform="hermes",
        binding_source="explicit",
    )
    assert first.session_hash == hash_session_id("raw-session-123")
    assert "raw-session-123" not in repr(first)

    repository.end("raw-session-123", source_agent="hermes-vm")
    second = repository.bind(
        "raw-session-123",
        scope="project:beta",
        source_agent="hermes-vm",
        platform="hermes",
        binding_source="explicit",
    )
    assert second.scope == "project:beta"
    assert len(repository.history("raw-session-123")) == 2

    with sqlite3.connect(service.store.db_path) as conn:
        payload = " ".join(
            str(value)
            for row in conn.execute("SELECT * FROM session_scope_bindings")
            for value in row
        )
    assert "raw-session-123" not in payload


def test_resolver_priority_resume_switch_and_no_implicit_global(tmp_path: Path) -> None:
    service = _service(tmp_path)
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    resolver = SessionScopeResolver(service.store.db_path)

    first = resolver.resolve(
        session_id="session-a",
        explicit_scope=None,
        workspace=alpha,
        source_agent="codex-session",
        platform="codex",
    )
    resumed = resolver.resolve(
        session_id="session-a",
        explicit_scope=None,
        workspace=beta,
        source_agent="codex-session",
        platform="codex",
    )
    switched = resolver.resolve(
        session_id="session-b",
        explicit_scope=None,
        workspace=beta,
        source_agent="codex-session",
        platform="codex",
    )
    unbound = resolver.resolve(
        session_id=None,
        explicit_scope=None,
        workspace=None,
        source_agent="codex-session",
        platform="codex",
    )

    assert (first.scope, first.scope_source) == ("project:alpha", "verified_workspace")
    assert (resumed.scope, resumed.scope_source) == ("project:alpha", "session_binding")
    assert switched.scope == "project:beta"
    assert (unbound.scope, unbound.scope_source) == ("user", "default_user")
    assert all(item.scope != "global" for item in (first, resumed, switched, unbound))


def test_explicit_scope_is_authorized_and_replaces_active_binding(tmp_path: Path) -> None:
    service = _service(tmp_path)
    workspace = tmp_path / "alpha"
    workspace.mkdir()
    resolver = SessionScopeResolver(service.store.db_path)
    resolver.resolve(
        session_id="session-a",
        explicit_scope=None,
        workspace=workspace,
        source_agent="hermes-vm",
        platform="hermes",
    )
    changed = resolver.resolve(
        session_id="session-a",
        explicit_scope="project:beta",
        workspace=workspace,
        source_agent="hermes-vm",
        platform="hermes",
        allowed_scopes={"project:alpha", "project:beta"},
    )
    assert (changed.scope, changed.scope_source) == ("project:beta", "explicit")
    assert len(SessionScopeRepository(service.store.db_path).history("session-a")) == 2

    with pytest.raises(PermissionError, match="outside the authorized scopes"):
        resolver.resolve(
            session_id="session-a",
            explicit_scope="global",
            workspace=workspace,
            source_agent="hermes-vm",
            platform="hermes",
            allowed_scopes={"project:alpha", "project:beta"},
        )


def test_explicit_confirmation_replaces_derived_binding_even_at_same_scope(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    workspace = tmp_path / "alpha"
    workspace.mkdir()
    resolver = SessionScopeResolver(service.store.db_path)
    resolver.resolve(
        session_id="session-a",
        explicit_scope=None,
        workspace=workspace,
        source_agent="hermes-vm",
        platform="hermes",
    )
    resolver.resolve(
        session_id="session-a",
        explicit_scope="project:alpha",
        workspace=workspace,
        source_agent="hermes-vm",
        platform="hermes",
    )
    history = SessionScopeRepository(service.store.db_path).history("session-a")
    assert [item.binding_source for item in history] == [
        "explicit",
        "verified_workspace",
    ]


def test_expired_binding_is_not_reused(tmp_path: Path) -> None:
    service = _service(tmp_path)
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    now = datetime(2026, 8, 7, tzinfo=timezone.utc)
    repository = SessionScopeRepository(service.store.db_path)
    repository.bind(
        "session-a",
        scope="project:alpha",
        source_agent="hermes-vm",
        platform="hermes",
        binding_source="verified_workspace",
        ttl_seconds=60,
        now=now,
    )
    resolved = SessionScopeResolver(service.store.db_path).resolve(
        session_id="session-a",
        explicit_scope=None,
        workspace=beta,
        source_agent="hermes-vm",
        platform="hermes",
        now=now + timedelta(seconds=61),
    )
    assert resolved.scope == "project:beta"
    assert resolved.scope_source == "verified_workspace"


def test_binding_metadata_rejects_secrets_before_persistence(tmp_path: Path) -> None:
    service = _service(tmp_path)
    repository = SessionScopeRepository(service.store.db_path)
    with pytest.raises(ValueError, match="sensitive"):
        repository.bind(
            "session-a",
            scope="project:alpha",
            source_agent="hermes-vm",
            platform="hermes",
            binding_source="explicit",
            task_label="token=super-secret-value",
        )
    assert repository.list_active() == []


def test_public_receipts_report_effective_session_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "alpha"
    workspace.mkdir()
    db = tmp_path / "public.db"
    captured = remember(
        text="Session scoped evidence.",
        session_id="session-a",
        source_agent="hermes-vm",
        platform="hermes",
        db=db,
        workspace=workspace,
    )
    recalled = recall(
        "Session scoped evidence",
        session_id="session-a",
        source_agent="hermes-vm",
        platform="hermes",
        db=db,
        workspace="",
    )
    assert (captured.scope, captured.scope_source) == (
        "project:alpha",
        "verified_workspace",
    )
    assert (recalled.scope, recalled.scope_source) == (
        "project:alpha",
        "session_binding",
    )


def test_repository_templates_have_no_no_cwd_global_fallback() -> None:
    root = Path(__file__).parents[1]
    hook = (
        root / "memorymaster/config_templates/hooks/memorymaster-auto-ingest.py"
    ).read_text(encoding="utf-8")
    session_end = (
        root / "memorymaster/surfaces/session_end_ingest.py"
    ).read_text(encoding="utf-8")
    assert 'if cwd else "global"' not in hook
    assert 'scope = "global" if not cwd' not in session_end
