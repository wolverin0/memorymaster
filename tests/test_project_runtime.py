from __future__ import annotations

import json
from types import SimpleNamespace

from datetime import datetime, timedelta, timezone

from test_project_repository import claim, project_config  # noqa: F401

from memorymaster.profile import project_runtime as runtime
from memorymaster.profile.project_projection import projection_paths, read_manifest, read_project_projection


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
    monkeypatch.setattr(runtime, "_refresh_one", lambda config, **kwargs: calls.append(config) or {"status": "ok"})
    runtime._refresh_cache.clear()
    runtime.refresh_project_projections(skip=True)
    runtime.refresh_project_projections(skip=True)
    assert calls == [cfg]


def test_cheap_cache_respects_temporal_boundary(monkeypatch):
    from datetime import datetime, timedelta, timezone

    cfg = SimpleNamespace(scope="project:fixture", context_key="fixture", epoch="test")
    monkeypatch.setattr(runtime, "active", lambda name: True)
    monkeypatch.setattr(runtime, "load_project_configs", lambda: [cfg])
    monkeypatch.setattr(runtime, "cheap_fingerprint", lambda config: ("unchanged",))
    calls = []
    def refresh(config, **kwargs):
        calls.append(config)
        return {"status": "ok", "next_temporal_boundary":
                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
    monkeypatch.setattr(runtime, "_refresh_one", refresh)
    runtime._refresh_cache.clear()
    runtime.refresh_project_projections(skip=True)
    runtime.refresh_project_projections(skip=True)
    assert calls == [cfg, cfg]


def test_scope_disagreement_and_wrong_repository_denied(tmp_path, monkeypatch):
    from memorymaster.core import scope_utils

    (tmp_path / ".git").mkdir()
    cfg = SimpleNamespace(project_root=tmp_path, scope="project:fixture", aliases=())
    monkeypatch.setattr(scope_utils, "scope_from_cwd_or_user", lambda cwd: "project:fixture")
    monkeypatch.setattr(scope_utils, "resolve_transcript_workspace_scope", lambda cwd: "project:wrong")
    assert runtime.resolve_projection_scope(cfg, tmp_path) is None


def test_enabled_unresolved_scope_never_requests_legacy_sql(monkeypatch):
    monkeypatch.setattr(runtime, "active", lambda name: True)
    monkeypatch.setattr(runtime, "config_for_cwd", lambda cwd: None)
    result = runtime.session_start_context({"cwd": "unresolved"})
    assert result is not None and "unavailable" in result.lower()


def test_skip_tick_after_complete_validation_renews_stale_read_ttl(project_config):  # noqa: F811
    """A SKIP tick that still ran a complete source validation renews checked_at once the stored
    check is older than half the 60-minute read TTL, so an unchanged projection never goes dark."""
    for number in range(1, 4):
        claim(project_config, number, text=f"Authorized decision {number}")
    assert runtime._refresh_one(project_config)["status"] == "ok"
    markdown_path, manifest_path = projection_paths(project_config)
    before = markdown_path.read_bytes()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    manifest["checked_at"] = stale.isoformat()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert read_project_projection(project_config) is None
    assert runtime._refresh_one(project_config, skip=True)["status"] == "ok"
    assert markdown_path.read_bytes() == before
    assert datetime.fromisoformat(read_manifest(project_config)["checked_at"]) > stale
    assert read_project_projection(project_config) is not None


def test_degraded_source_publishes_only_for_a_brief_and_never_replaces_a_young_complete_publication():
    config = SimpleNamespace(brief=True)
    manifest = {"brief": True, "source_complete": False}
    now = datetime.now(timezone.utc)
    stamp = lambda minutes: (now - timedelta(minutes=minutes)).isoformat()  # noqa: E731
    assert runtime._publish_degraded(config, manifest, None) is True
    assert runtime._publish_degraded(config, manifest, {"source_complete": False, "checked_at": stamp(1)}) is True
    assert runtime._publish_degraded(config, manifest, {"source_complete": True, "checked_at": stamp(5)}) is False
    assert runtime._publish_degraded(config, manifest, {"source_complete": True, "checked_at": stamp(45)}) is True
    assert runtime._publish_degraded(SimpleNamespace(brief=False), manifest, None) is False  # brief off: unchanged
    assert runtime._publish_degraded(config, {"source_complete": False}, None) is False  # no brief in the manifest


# --- fair refresh order across processes (the refresher is a NEW process on every Beat tick) ---

_T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


class _Disk:
    """What the persisted manifests say (context_key -> ISO checked_at, None for absent, 'raise' for unreadable),
    on a fake wall clock that moves one millisecond per reading, so every stamp orders strictly."""

    def __init__(self, tmp_path, keys, initial=None):
        self.out = tmp_path
        self.checked = {key: initial for key in keys}
        self.ticks = 0
        self.clock = 0.0
        self.refreshed: list[str] = []
        self.attempts: list[str] = []
        self.pending: set[str] = set()
        self.reads = 0

    def now(self):
        self.reads += 1
        return _T0 + timedelta(milliseconds=self.reads)

    def read_manifest(self, config):
        value = self.checked[config.context_key]
        if value == "raise":
            raise ValueError("corrupt manifest")
        return None if value is None else {"checked_at": value}

    def refresh_one(self, config, **kwargs):
        self.attempts.append(config.context_key)
        self.clock += 1.0  # every build exceeds the 750 ms tick budget
        if config.context_key in self.pending:
            return {"status": "pending"}
        self.refreshed.append(config.context_key)
        self.checked[config.context_key] = self.now().isoformat()
        return {"status": "ok"}


def _fair_setup(monkeypatch, disk, keys):
    from memorymaster.profile import project_projection

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return disk.now()

    configs = [SimpleNamespace(scope=f"project:{key}", context_key=key, epoch="test", slug=key,
                               output_root=disk.out) for key in keys]
    monkeypatch.setattr(runtime, "active", lambda name: True)
    monkeypatch.setattr(runtime, "load_project_configs", lambda: configs)
    monkeypatch.setattr(runtime, "cheap_fingerprint", lambda config: ("changed",))
    monkeypatch.setattr(runtime, "_refresh_one", disk.refresh_one)
    monkeypatch.setattr(runtime, "datetime", Clock)
    monkeypatch.setattr(project_projection, "read_manifest", disk.read_manifest)
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: disk.clock))
    return configs


def _new_process():
    """A scheduled-task tick starts with every module global at its import-time value."""
    for connection in runtime._connections.values():
        connection.close()
    runtime._connections.clear()
    runtime._refresh_cache.clear()
    runtime._rotation = 0


def _run_ticks(disk, count):
    for _ in range(count):
        _new_process()
        disk.ticks += 1
        runtime.refresh_project_projections()


def _assert_every_project_served_each_round(order, keys, rounds_of):
    for key in keys:
        ticks = [i + 1 for i, name in enumerate(order) if name == key]
        assert ticks and ticks[0] <= rounds_of, (key, order)
        assert all(later - earlier <= rounds_of for earlier, later in zip(ticks, ticks[1:])), (key, order)


def test_budget_exhausted_every_tick_still_refreshes_every_project_in_turn(tmp_path, monkeypatch):
    disk = _Disk(tmp_path, "abcd", initial=(_T0 - timedelta(hours=7)).isoformat())
    _fair_setup(monkeypatch, disk, "abcd")
    _run_ticks(disk, 12)
    assert set(disk.refreshed) == set("abcd")
    _assert_every_project_served_each_round(disk.refreshed, "abcd", 4)  # waits no more than len(configs) ticks


def test_project_without_manifest_goes_before_one_with_an_old_manifest(tmp_path, monkeypatch):
    disk = _Disk(tmp_path, "abcd", initial=_T0.isoformat())
    disk.checked["a"] = (_T0 - timedelta(hours=7)).isoformat()  # old manifest, first in config order
    disk.checked["c"] = None  # no manifest at all
    _fair_setup(monkeypatch, disk, "abcd")
    _run_ticks(disk, 1)
    assert disk.refreshed == ["c"]


def test_corrupt_or_unreadable_manifest_counts_as_stalest_and_never_crashes_the_tick(tmp_path, monkeypatch):
    disk = _Disk(tmp_path, "abcd", initial=(_T0 - timedelta(hours=1)).isoformat())
    disk.checked["b"] = (_T0 - timedelta(hours=7)).isoformat()
    disk.checked["d"] = "raise"
    _fair_setup(monkeypatch, disk, "abcd")
    _run_ticks(disk, 1)
    assert disk.refreshed == ["d"]
    disk.checked["d"] = "not-a-timestamp"  # parseable manifest shape, garbage instant
    disk.checked["c"] = "2026-11-01T00:00:00"  # naive instant is not comparable with aware ones
    disk.refreshed.clear()
    _run_ticks(disk, 1)
    assert disk.refreshed[0] in {"d", "c"}


def test_equal_staleness_keeps_the_in_process_rotation_as_tie_break(tmp_path, monkeypatch):
    disk = _Disk(tmp_path, "abc", initial=_T0.isoformat())
    _fair_setup(monkeypatch, disk, "abc")
    _new_process()
    runtime._rotation = 1
    runtime.refresh_project_projections()
    assert disk.refreshed == ["b"]


def test_future_dated_manifest_is_the_stalest_and_cannot_starve_its_project(tmp_path, monkeypatch):
    """Clock skew, a restored file or a bad write must not make a project the newest forever."""
    disk = _Disk(tmp_path, "abcd", initial=(_T0 - timedelta(hours=2)).isoformat())
    disk.checked["c"] = (_T0 + timedelta(days=1)).isoformat()
    _fair_setup(monkeypatch, disk, "abcd")
    _run_ticks(disk, 1)
    assert disk.refreshed == ["c"]
    assert datetime.fromisoformat(disk.checked["c"]) < _T0 + timedelta(hours=1)  # sane stamp after the tick
    _run_ticks(disk, 7)
    _assert_every_project_served_each_round(disk.refreshed, "abcd", 4)


def test_future_dated_manifest_is_not_served_as_fresh_and_a_tick_gives_it_a_sane_checked_at(
        project_config):  # noqa: F811
    claim(project_config)
    assert runtime._refresh_one(project_config)["status"] == "ok"
    _, manifest_path = projection_paths(project_config)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for skip in (False, True):  # a skip tick with an unchanged hint must renew it as well
        manifest["checked_at"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        assert read_project_projection(project_config) is None  # reader: a future instant is not fresh
        assert runtime._refresh_one(project_config, skip=skip)["status"] == "ok"
        assert datetime.fromisoformat(read_manifest(project_config)["checked_at"]) <= datetime.now(timezone.utc)
        assert read_project_projection(project_config) is not None


def test_permanently_pending_project_neither_starves_the_others_nor_stops_being_retried(tmp_path, monkeypatch):
    disk = _Disk(tmp_path, "abcd", initial=(_T0 - timedelta(hours=7)).isoformat())
    disk.pending = {"a"}  # first in config order, as in the live defect
    _fair_setup(monkeypatch, disk, "abcd")
    _run_ticks(disk, 12)
    assert {"b", "c", "d"} <= set(disk.refreshed)
    _assert_every_project_served_each_round(disk.attempts, "abcd", 4)  # the pending one is retried each round
    _assert_every_project_served_each_round(disk.refreshed, "bcd", 4)
    disk.pending.clear()  # recovery: it publishes normally on its next turn
    _run_ticks(disk, 4)
    assert "a" in disk.refreshed and disk.checked["a"] is not None


def test_attempt_stamp_never_renews_the_read_ttl_nor_changes_what_a_session_receives(
        project_config, monkeypatch):  # noqa: F811
    claim(project_config)
    assert runtime._refresh_one(project_config)["status"] == "ok"
    out = project_config.output_root
    manifest_path = projection_paths(project_config)[1]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["checked_at"] = (datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = sorted(p for p in out.rglob("*") if p.is_file() and p.suffix != ".lock")
    manifest_bytes = manifest_path.read_bytes()
    monkeypatch.setattr(runtime, "active", lambda name: name != "MEMORYMASTER_PROJECT_SESSION_DELTA")
    monkeypatch.setattr(runtime, "load_project_configs", lambda: [project_config])
    monkeypatch.setattr(runtime, "config_for_cwd", lambda cwd: project_config)
    monkeypatch.setattr(runtime, "_refresh_one", lambda config, **kwargs: {"status": "pending"})
    _new_process()
    assert runtime.refresh_project_projections()["status"] == "pending"
    added = [p for p in out.rglob("*") if p.is_file() and p.suffix != ".lock" and p not in before]
    assert len(added) == 1 and added[0].stat().st_size < 256  # one small attempt file beside the projection
    assert manifest_path.read_bytes() == manifest_bytes  # the manifest is untouched
    assert read_project_projection(project_config) is None  # and the read TTL is not renewed
    assert runtime.session_start_context({"cwd": "x"}) == runtime.compose_session_context(
        "Project context unavailable; refresh pending.")
    added[0].write_bytes(b"\xff not json")  # a broken attempt file means "no attempt"; the tick survives
    _new_process()
    assert runtime.refresh_project_projections()["status"] == "pending"


def test_all_fresh_and_unchanged_projects_tick_ok_without_any_write(project_config, tmp_path):  # noqa: F811
    from dataclasses import replace

    claim(project_config)
    second = replace(project_config, context_key="fixture-two")
    for config in (project_config, second):
        assert runtime._refresh_one(config)["status"] == "ok"
    _new_process()
    import os
    from unittest import mock

    def snapshot():
        return sorted((p.name, p.read_bytes(), p.stat().st_mtime_ns)
                      for p in project_config.output_root.rglob("*") if p.is_file())
    before = snapshot()
    env = {runtime.PREFIX + "ENABLED": "1", runtime.PREFIX + "REFRESH": "1"}
    with mock.patch.dict(os.environ, env), mock.patch.object(
            runtime, "load_project_configs", lambda: [project_config, second]):
        result = runtime.refresh_project_projections(skip=True)
    assert result["status"] == "ok" and [r["status"] for r in result["projects"]] == ["unchanged"] * 2
    assert snapshot() == before


def test_skip_tick_that_rewrites_the_hint_also_advances_checked_at(project_config):  # noqa: F811
    """Fairness needs a refreshed project to move to the back: a validated project must not stay 'stalest'."""
    claim(project_config)
    assert runtime._refresh_one(project_config)["status"] == "ok"
    _, manifest_path = projection_paths(project_config)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ten_minutes_ago = datetime.now(timezone.utc) - timedelta(minutes=10)
    manifest["checked_at"] = ten_minutes_ago.isoformat()
    manifest["refresh_hint"] = {"sha256": "outdated"}  # the cheap fingerprint moved, the content did not
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert runtime._refresh_one(project_config, skip=True)["status"] == "ok"
    assert datetime.fromisoformat(read_manifest(project_config)["checked_at"]) > ten_minutes_ago


def test_expired_projection_of_a_configured_project_delivers_only_the_refresh_pending_notice(
        project_config, monkeypatch):  # noqa: F811
    """Documented read-side behaviour: past the 60-minute read TTL nothing of the brief is delivered."""
    claim(project_config)
    assert runtime._refresh_one(project_config)["status"] == "ok"
    monkeypatch.setattr(runtime, "active", lambda name: name != "MEMORYMASTER_PROJECT_SESSION_DELTA")
    monkeypatch.setattr(runtime, "config_for_cwd", lambda cwd: project_config)
    fresh = runtime.session_start_context({"cwd": "x"})
    assert "refresh pending" not in fresh and len(fresh.encode()) > 300
    _, manifest_path = projection_paths(project_config)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["checked_at"] = (datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    expired = runtime.session_start_context({"cwd": "x"})
    assert expired == runtime.compose_session_context("Project context unavailable; refresh pending.")
    assert len(expired.encode()) == 162
