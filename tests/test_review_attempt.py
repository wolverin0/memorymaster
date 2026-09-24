"""Timeouts and interrupted/partial attempts cannot borrow yesterday's PASS."""

import json
import runpy
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from memorymaster.operations.review_attempt import atomic_json, phase_progress, read_json, read_review_state
from memorymaster.operations.review_supervisor import supervise

NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def _attempt():
    return {"attempt_id": "current", "started_at": NOW.isoformat(),
            "deadline_at": (NOW + timedelta(minutes=24)).isoformat(),
            "completed_at": None, "interval_seconds": 3600, "outcome": "INCOMPLETE"}


def test_external_interruption_deadline_and_old_result_are_independent(tmp_path):
    old = {"attempt_id": "old", "verdict": "PASS", "observed_at": (NOW - timedelta(hours=2)).isoformat()}
    atomic_json(tmp_path / "latest.json", old)
    atomic_json(tmp_path / "attempt.json", _attempt())
    assert read_review_state(tmp_path, now=NOW)["verdict"] == "INCOMPLETE"
    late = read_review_state(tmp_path, now=NOW + timedelta(minutes=24))
    assert late["verdict"] == "TIMEOUT"
    assert late["last_completed"]["verdict"] == "PASS"
    assert read_json(tmp_path / "latest.json") == old


@pytest.mark.parametrize("mode", ["timeout", "interrupt", "partial", "complete"])
def test_supervisor_only_kills_its_child_and_preserves_latest_on_failure(tmp_path, mode):
    old = {"verdict": "PASS", "attempt_id": "yesterday"}
    atomic_json(tmp_path / "latest.json", old)
    killed = []

    class Child:
        def __init__(self, command, *, stdout, stderr, env, **_kwargs):
            assert command[:3] == [sys.executable, "-m", "memorymaster.operations.operational_review"]
            self.done = False
            if mode == "complete":
                json.dump({"schema": "memorymaster.operational-review.v1", "attempt_id": env["MEMORYMASTER_REVIEW_ATTEMPT_ID"],
                           "review_performed": True, "exit_code": 0, "verdict": "PASS", "checks": [{}] * 9}, stdout)
            else:
                stdout.write('{"partial":')

        def wait(self, timeout=None):
            if not self.done and mode == "timeout":
                raise subprocess.TimeoutExpired("owned child", timeout)
            if not self.done and mode == "interrupt":
                raise KeyboardInterrupt()
            return 0

        def kill(self):
            killed.append(self)
            self.done = True

    config = {"python": sys.executable, "db": "fixture.db", "output_root": str(tmp_path), "every_hours": 1}
    code = supervise(config, timeout_seconds=0.1, popen=Child, now=lambda: NOW)
    assert len(killed) == (1 if mode in {"timeout", "interrupt"} else 0)
    assert code == {"timeout": 124, "interrupt": 9, "partial": 9, "complete": 0}[mode]
    state = read_review_state(tmp_path, now=NOW)
    assert state["verdict"] == {"timeout": "TIMEOUT", "interrupt": "INCOMPLETE", "partial": "INCOMPLETE", "complete": "PASS"}[mode]
    if mode != "complete":
        assert read_json(tmp_path / "latest.json") == old


def test_configured_freshness_plus_five_minutes(tmp_path):
    attempt = {**_attempt(), "completed_at": NOW.isoformat(), "outcome": "PASS"}
    atomic_json(tmp_path / "attempt.json", attempt)
    atomic_json(tmp_path / "latest.json", {"attempt_id": "current", "verdict": "PASS"})
    assert read_review_state(tmp_path, now=NOW + timedelta(minutes=65))["verdict"] == "PASS"
    assert read_review_state(tmp_path, now=NOW + timedelta(minutes=65, seconds=1))["verdict"] == "STALE"
    assert read_review_state(tmp_path, now=NOW + timedelta(minutes=66), interval_seconds=7200)["verdict"] == "PASS"
    (tmp_path / "attempt.json").write_text("{", encoding="utf-8")
    assert read_review_state(tmp_path, now=NOW)["verdict"] == "INCOMPLETE"


def test_phase_records_are_bound_to_current_attempt(tmp_path, monkeypatch):
    path = tmp_path / "attempt.json"
    atomic_json(path, _attempt())
    monkeypatch.setenv("MEMORYMASTER_REVIEW_ATTEMPT_FILE", str(path))
    monkeypatch.setenv("MEMORYMASTER_REVIEW_ATTEMPT_ID", "current")
    phase_progress("database")
    phase_progress("database", 1.2345)
    assert read_json(path)["phase_seconds"] == {"database": 1.234}
    monkeypatch.setenv("MEMORYMASTER_REVIEW_ATTEMPT_ID", "old")
    phase_progress("stale_writer", 50)
    assert read_json(path)["phase"] == "database"


def test_actual_child_runs_all_existing_review_phases_on_disposable_db(tmp_path, monkeypatch):
    helper = runpy.run_path("tests/test_operational_review.py")
    db = helper["_db"](tmp_path / "fixture.db")
    root = tmp_path / "results"
    # The child inherits the environment: keep its Jev ledger and checkpoint log disposable.
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(tmp_path / "decisions.db"))
    monkeypatch.setenv("MEMORYMASTER_CHECKPOINT_LOG", str(tmp_path / "feature-checkpoint.log"))
    code = supervise({"python": sys.executable, "db": str(db), "output_root": str(root)}, timeout_seconds=20)
    assert code == 3  # no retrieval canary configured, never pretend PASS
    result = read_json(root / "latest.json")
    assert len(result["checks"]) == 9
    assert [check["name"] for check in result["checks"]][-2:] == ["jev_decisions", "checkpoint_delivery"]
    assert len(read_json(root / "attempt.json")["phase_seconds"]) == 9
    assert read_review_state(root)["verdict"] == "WARN"


def test_absent_checkpoint_pane_cannot_borrow_recent_work_receipt():
    gate = runpy.run_path("scripts/run_v47_operational_acceptance.py")
    result = gate["checkpoint_result"](
        "MemoryMaster-Checkpoint-Daily",
        {"enabled": True, "last_result": 4, "last_run": NOW.isoformat()},
        [{"task": "MemoryMaster-Checkpoint-Daily", "work_performed": True,
          "result": "pass", "completed_at": (NOW - timedelta(hours=1)).isoformat()}], NOW,
    )
    assert result.verdict.value == "FAIL"
    assert "delivered=false completed=false" in result.detail
