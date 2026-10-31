"""AO-2 v1 beat: the scheduled-task tick (T-1640 AC3). Hermetic: state db, logs and artifacts in tmp."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memorymaster.dreaming.latency_guard import GuardDecision
from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.surfaces import scheduled_task


@pytest.fixture()
def beat(tmp_path: Path, monkeypatch):
    state = tmp_path / "state" / "capture-control.db"
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_STATE_DB", str(state))
    monkeypatch.delenv("MEMORYMASTER_BEAT_ENABLED", raising=False)
    monkeypatch.setattr(scheduled_task, "_log_path", lambda mode: tmp_path / f"{mode}.log")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[tuple[str, object]] = []
    decision = {"action": "allow"}

    def fake_scan(ledger, **kwargs):
        calls.append(("scan", kwargs))
        return {"scanned": 0, "queued": 0, "errors": 0, "skipped_no_cwd": 0}

    def fake_evaluate(*_args, **_kwargs):
        calls.append(("guard", decision["action"]))
        return GuardDecision(decision["action"], [f"test_{decision['action']}"], {})

    def fake_dream(db, workspace_, **kwargs):
        calls.append(("dream", kwargs))
        return {"ok": True, "errors": 0}

    monkeypatch.setattr("memorymaster.dreaming.capture.scan_active_transcripts", fake_scan)
    monkeypatch.setattr("memorymaster.dreaming.latency_guard.evaluate", fake_evaluate)
    monkeypatch.setattr("memorymaster.dreaming.worker.run_dream", fake_dream)
    argv = ["beat", "--db", str(tmp_path / "memory.db"), "--workspace", str(workspace)]
    return {"argv": argv, "calls": calls, "decision": decision, "state": state, "tmp": tmp_path,
            "workspace": workspace}


def test_with_the_switch_off_the_beat_does_nothing(beat) -> None:
    assert scheduled_task.main(beat["argv"]) == 0

    assert beat["calls"] == []
    assert not beat["state"].exists()  # not even the state db is created
    assert not (beat["workspace"] / "artifacts").exists()


def test_switch_off_leaves_the_existing_dream_task_untouched(beat, monkeypatch) -> None:
    """The beat code is inert unless asked for: `dream` mode never scans, guards or reads the switch."""
    monkeypatch.setenv("MEMORYMASTER_BEAT_ENABLED", "1")  # even ON, the dream mode is unchanged
    seen: list[str] = []
    monkeypatch.setattr(scheduled_task, "_run_dream", lambda args: seen.append(args.mode) or 0)

    assert scheduled_task.main(["dream", "--db", "x.db", "--workspace", str(beat["workspace"])]) == 0

    assert seen == ["dream"] and beat["calls"] == []


@pytest.mark.parametrize("value", ["0", "false", "off", "", "no"])
def test_falsy_switch_values_keep_the_beat_off(beat, monkeypatch, value) -> None:
    monkeypatch.setenv("MEMORYMASTER_BEAT_ENABLED", value)

    assert scheduled_task.main(beat["argv"]) == 0
    assert beat["calls"] == []


def test_allow_asks_the_guard_then_scans_then_runs_a_bounded_dream(beat, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_BEAT_ENABLED", "1")

    assert scheduled_task.main(beat["argv"]) == 0

    assert [name for name, _ in beat["calls"]] == ["guard", "scan", "dream"]
    dream_kwargs = beat["calls"][2][1]
    config = dream_kwargs["config"]
    assert config.max_sessions == 3
    assert config.max_consolidate_captures_per_run == 3
    assert config.oldest_reserve == 1
    assert config.lease_ttl_seconds == 270
    assert config.max_input_tokens_daily == 2_000_000  # the shared daily ceiling is kept
    assert dream_kwargs["apply_candidates"] is False  # applying claims is a separate activation


def test_capture_only_scans_but_never_consolidates(beat, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_BEAT_ENABLED", "yes")
    beat["decision"]["action"] = "capture_only"

    assert scheduled_task.main(beat["argv"]) == 0

    assert [name for name, _ in beat["calls"]] == ["guard", "scan"]


def test_skip_only_logs_and_touches_no_transcript_and_no_ledger(beat, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_BEAT_ENABLED", "true")
    beat["decision"]["action"] = "skip"

    assert scheduled_task.main(beat["argv"]) == 0

    # A hard latency breach must not scan directories, probe files or write the state db.
    assert [name for name, _ in beat["calls"]] == ["guard"]
    assert not beat["state"].exists()
    log = (beat["tmp"] / "beat.log").read_text(encoding="utf-8")
    assert "beat_tick" in log and "skip" in log


def test_a_busy_dream_worker_lease_makes_the_tick_a_noop(beat, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_BEAT_ENABLED", "1")
    assert DreamLedger(beat["state"]).acquire_lease("dream-worker", "hourly-dreaming", 900)

    assert scheduled_task.main(beat["argv"]) == 0

    assert [name for name, _ in beat["calls"]] == ["guard"]  # no scan, no dream


def test_the_lease_is_free_again_when_the_tick_ends(beat, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_BEAT_ENABLED", "1")
    scheduled_task.main(beat["argv"])

    with sqlite3.connect(beat["state"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dream_leases WHERE name='dream-worker'").fetchone()[0] == 0


def test_a_failed_dream_run_fails_the_task(beat, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_BEAT_ENABLED", "1")
    monkeypatch.setattr("memorymaster.dreaming.worker.run_dream",
                        lambda *_a, **_k: {"ok": False, "errors": 1, "fatal": "boom"})

    assert scheduled_task.main(beat["argv"]) == 1


def test_worker_busy_from_the_hourly_task_is_not_a_failure(beat, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_BEAT_ENABLED", "1")
    monkeypatch.setattr("memorymaster.dreaming.worker.run_dream",
                        lambda *_a, **_k: {"ok": False, "reason": "worker_busy"})

    assert scheduled_task.main(beat["argv"]) == 0


def test_an_enabled_tick_writes_the_measurement_artifact(beat, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_BEAT_ENABLED", "1")

    scheduled_task.main(beat["argv"])

    files = list((beat["workspace"] / "artifacts").glob("*-ao2-beat-measurements.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["schema"] == "memorymaster.ao2-beat-measurements.v1"
    assert {"m3", "m4"} <= set(payload)


def test_dreamconfig_per_run_capture_cap_defaults_to_unlimited() -> None:
    from memorymaster.dreaming.worker import DreamConfig

    assert DreamConfig().max_consolidate_captures_per_run == 0
    assert DreamConfig.from_env().max_consolidate_captures_per_run == 0
