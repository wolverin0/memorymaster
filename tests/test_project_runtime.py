from __future__ import annotations

import json
from types import SimpleNamespace

from memorymaster.profile import project_runtime as runtime


def test_all_gates_off_before_any_paths(monkeypatch):
    monkeypatch.delenv("MEMORYMASTER_PROJECT_PROJECTION_ENABLED", raising=False)
    monkeypatch.setenv("MEMORYMASTER_PROJECT_PROJECTION_PROJECTS", "broken")
    assert runtime.load_project_configs() == []
    assert runtime.session_start_context({"cwd": "/nonexistent"}) is None
    assert runtime.record_session_end({}) is False
    assert runtime.refresh_project_projections() == {"status": "inactive"}


def test_child_gates_cannot_bypass_master(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_PROJECT_PROJECTION_ENABLED", "0")
    for suffix in ("REFRESH", "SESSION_START", "COCKPIT"):
        monkeypatch.setenv("MEMORYMASTER_PROJECT_PROJECTION_" + suffix, "1")
    monkeypatch.setenv("MEMORYMASTER_PROJECT_SESSION_DELTA", "1")
    assert runtime.load_project_configs() == []
    assert runtime.session_start_context({}) is None
    assert runtime.refresh_project_projections() == {"status": "inactive"}


def test_session_start_total_and_single_envelope():
    payload = "# Project\n" + "x" * 2050
    delta = "é" * 240
    profile = runtime.GENERATED_OPENER + "\n" + "\n".join("- " + "z" * 90 for _ in range(20))
    context = runtime.compose_session_context(payload, delta, profile)
    assert len(context) <= 3000
    assert context.count(runtime.GENERATED_OPENER) == context.count(runtime.GENERATED_CLOSER) == 1
    assert payload in context
    assert "profile_omitted=" in context


def test_allowlist_requires_explicit_roots_epoch_and_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_PROJECT_PROJECTION_ENABLED", "1")
    monkeypatch.setenv("MEMORYMASTER_PROJECT_PROJECTION_PROJECTS", json.dumps([
        {"scope": "project:fixture", "context_key": "fixture", "project_root": str(tmp_path),
         "db_path": str(tmp_path / "fixture.db"), "tenant_id": None}]))
    assert runtime.load_project_configs() == []
    monkeypatch.setenv("MEMORYMASTER_PROJECT_PROJECTION_DIR", str(tmp_path / "projections"))
    monkeypatch.setenv("MEMORYMASTER_PROJECT_SESSION_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MEMORYMASTER_PROJECT_PROJECTION_EPOCH", "fixture-epoch")
    assert len(runtime.load_project_configs()) == 1


def test_skip_refresh_prechecks_cheap_fingerprints_without_full_scan(tmp_path, monkeypatch):
    cfg = SimpleNamespace(scope="project:fixture", context_key="fixture", epoch="test")
    monkeypatch.setattr(runtime, "active", lambda name: True)
    monkeypatch.setattr(runtime, "load_project_configs", lambda: [cfg])
    monkeypatch.setattr(runtime, "cheap_fingerprint", lambda config: ("unchanged",))
    calls = []
    monkeypatch.setattr(runtime, "_refresh_one", lambda config: calls.append(config) or {"status": "ok"})
    runtime._refresh_cache.clear()
    runtime.refresh_project_projections(skip=True)
    runtime.refresh_project_projections(skip=True)
    assert calls == [cfg]


def test_scope_disagreement_and_wrong_repository_denied(tmp_path, monkeypatch):
    from memorymaster.core import scope_utils

    (tmp_path / ".git").mkdir()
    cfg = SimpleNamespace(project_root=tmp_path, scope="project:fixture", aliases=())
    monkeypatch.setattr(scope_utils, "scope_from_cwd_or_user", lambda cwd: "project:fixture")
    monkeypatch.setattr(scope_utils, "resolve_transcript_workspace_scope", lambda cwd: "project:wrong")
    assert runtime.resolve_projection_scope(cfg, tmp_path) is None
