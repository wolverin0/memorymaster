from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sqlite3
import sys

from test_project_repository import claim, project_config as _project_config

from memorymaster.operations.operational_review import ReviewConfig, Verdict, check_project_projection
from memorymaster.profile import project_repository, project_runtime
from memorymaster.profile.project_projection import (
    build_project_projection, projection_paths, publish_project_projection, read_project_projection,
)
from memorymaster.profile.project_sessions import record_project_session_start

project_config = _project_config


def _hook(name):
    path = Path(__file__).resolve().parents[1] / "memorymaster/config_templates/hooks" / name
    spec = importlib.util.spec_from_file_location("ao3_" + name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_m2_three_repo_probe(project_config, tmp_path, monkeypatch, capsys):
    import pytest

    configs = []
    for index in range(3):
        root = tmp_path / f"repo{index}"
        root.mkdir()
        (root / ".git").mkdir()
        (root / ".git/HEAD").write_text("ref: refs/heads/main\n")
        db = tmp_path / f"repo{index}.sqlite"
        shutil.copyfile(project_config.db_path, db)
        ledger = tmp_path / f"ledger{index}"
        ledger.mkdir()
        cfg = replace(project_config, project_root=root, scope=f"project:repo{index}",
                      db_path=db, ledger_root=ledger, ledger_repo=f"repo{index}")
        for number in range(1, 4):
            claim(cfg, number, scope=cfg.scope, text=f"Repository {index} decision {number}")
        claim(cfg, 4, scope=cfg.scope, status="candidate", text=f"Late confirmed choice {index}")
        (ledger / "tasks.json").write_text(json.dumps({"complete": True, "tasks": [
            {"id": f"T-{index}", "repo": cfg.ledger_repo, "state": "ready", "title": f"Visible work {index}"}]}))
        assert publish_project_projection(cfg, build_project_projection(cfg))
        configs.append(cfg)
    for suffix in ("ENABLED", "SESSION_START"):
        monkeypatch.setenv(project_runtime.PREFIX + suffix, "1")
    monkeypatch.setenv("MEMORYMASTER_PROJECT_SESSION_DELTA", "1")
    monkeypatch.setenv("MEMORYMASTER_SESSION_END_CAPTURE", "0")
    monkeypatch.setattr(project_runtime, "load_project_configs", lambda: configs)
    start = _hook("memorymaster-session-start.py")
    end = _hook("memorymaster-session-end.py")
    monkeypatch.setattr(start, "_log", lambda *args, **kwargs: None)
    # The enabled branch has no independent claim/wiki SQL or recall/model fallback.
    monkeypatch.setattr(start, "_query_recent_claims", lambda *args: pytest.fail("legacy SQL used"))
    for cfg in configs:
        payload = {"cwd": str(cfg.project_root), "session_id": cfg.scope + "-S"}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        with pytest.raises(SystemExit):
            start.main()
        first = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        for number in range(1, 4):
            assert f"decision {number}" in first
        with sqlite3.connect(cfg.db_path) as connection:
            connection.execute("UPDATE claims SET status='confirmed',updated_at=? WHERE id=4",
                               (datetime.now(timezone.utc).isoformat(),))
        path = cfg.ledger_root / "tasks.json"
        current = json.loads(path.read_text())
        current["tasks"][0].update(state="running", title="Edited visible work")
        path.write_text(json.dumps(current))
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        end.main()
        payload["session_id"] += "-next"
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        with pytest.raises(SystemExit):
            start.main()
        context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        shared = read_project_projection(cfg)
        assert shared.payload in context  # Same reader contract used by the external Cockpit owner.
        assert "Late confirmed choice" in context
        assert "Edited visible work" in context
        assert "open_cards_trimmed" not in context
        assert "prior-session" in context.lower()
        assert len(context) <= 3000
        assert context.count(project_runtime.GENERATED_OPENER) == context.count(project_runtime.GENERATED_CLOSER) == 1


def test_session_start_baseline_never_full_source_scan(project_config, monkeypatch):
    claim(project_config)
    projection = build_project_projection(project_config)
    observed = []
    original = project_repository._read_claims
    def bounded(config, **kwargs):
        observed.append(kwargs)
        assert kwargs["ids"] is not None
        assert kwargs["changed_since"] == projection.manifest["read_at"]
        return original(config, **kwargs)
    monkeypatch.setattr(project_repository, "_read_claims", bounded)
    result = record_project_session_start(project_config, "fixture", projection=projection.manifest)
    assert result["status"] == "ok"
    assert len(observed) == 1


def test_skip_unchanged_refresh_keeps_files_and_checked_at(project_config):
    claim(project_config)
    assert project_runtime._refresh_one(project_config)["status"] == "ok"
    paths = projection_paths(project_config)
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
    assert project_runtime._refresh_one(project_config, skip=True)["status"] == "ok"
    assert before == [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]


def test_skip_refresh_survives_a_fresh_scheduler_process(project_config, monkeypatch):
    claim(project_config)
    assert project_runtime._refresh_one(project_config)["status"] == "ok"
    project_runtime._refresh_cache.clear()
    for connection in project_runtime._connections.values():
        connection.close()
    project_runtime._connections.clear()
    monkeypatch.setattr(project_runtime, "active", lambda name: True)
    monkeypatch.setattr(project_runtime, "load_project_configs", lambda: [project_config])
    def forbidden(*args, **kwargs):
        raise AssertionError("unchanged SKIP performed full scan in fresh process")
    monkeypatch.setattr(project_runtime, "_refresh_one", forbidden)
    assert project_runtime.refresh_project_projections(skip=True)["status"] == "ok"


def test_projection_review_never_fabricates_real_m2_receipt(project_config, monkeypatch):
    for number in range(1, 4):
        claim(project_config, number)
    assert publish_project_projection(project_config, build_project_projection(project_config))
    monkeypatch.setenv(project_runtime.PREFIX + "ENABLED", "1")
    result = check_project_projection(ReviewConfig(db=project_config.db_path,
        project_projection_configs=(project_config,)))
    assert result.verdict == Verdict.WARN
    assert result.counts["m2_passed"] == 0
    assert "real_m2_probe_pending" in result.detail
    _, manifest_path = projection_paths(project_config)
    manifest = json.loads(manifest_path.read_text())
    manifest["checked_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    manifest_path.write_text(json.dumps(manifest))
    assert read_project_projection(project_config) is None
