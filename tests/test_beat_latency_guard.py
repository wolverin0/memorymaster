"""AO-2 v1 beat: latency guard (T-1640 AC2). Every path is injected; nothing touches the home dir."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.dreaming import latency_guard
from memorymaster.dreaming.latency_guard import evaluate

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _default_thresholds(monkeypatch) -> None:
    for name in [key for key in __import__("os").environ if key.startswith("MEMORYMASTER_BEAT_")]:
        monkeypatch.delenv(name, raising=False)


def _dense_log(path: Path, totals: list[float], *, age_minutes: float = 5) -> None:
    stamp = (NOW - timedelta(minutes=age_minutes)).isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as handle:
        for total in totals:
            handle.write(json.dumps({"ts": stamp, "outcome": "dense", "service_ms": total / 2, "total_ms": total}) + "\n")


def _hook_log(path: Path, totals: list[float], *, age_minutes: float = 5) -> None:
    stamp = (NOW - timedelta(minutes=age_minutes)).astimezone().isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as handle:
        for total in totals:
            handle.write(f"[{stamp}] hook=recall event=latency_total total_ms={total} dense_ms=12.5\n")
        handle.write(f"[{stamp}] hook=recall event=done injected=2\n")  # no duration: ignored


def _run(tmp_path: Path, **kwargs):
    return evaluate(
        NOW,
        dense_log=kwargs.pop("dense_log", tmp_path / "dense.jsonl"),
        hook_log=kwargs.pop("hook_log", tmp_path / "hook.log"),
        guard_log=kwargs.pop("guard_log", tmp_path / "guard.jsonl"),
        **kwargs,
    )


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_missing_logs_allow_and_the_decision_is_still_logged(tmp_path: Path) -> None:
    decision = _run(tmp_path)

    assert decision.action == "allow"
    events = _events(tmp_path / "guard.jsonl")
    assert len(events) == 1 and events[0]["action"] == "allow"
    assert events[0]["reasons"] == ["insufficient_data"]


def test_cold_start_with_fewer_than_min_events_allows_even_when_slow(tmp_path: Path) -> None:
    _dense_log(tmp_path / "dense.jsonl", [900.0, 950.0])  # only 2 events, very slow

    assert _run(tmp_path).action == "allow"


def test_healthy_dense_and_hook_latency_allows(tmp_path: Path) -> None:
    _dense_log(tmp_path / "dense.jsonl", [80.0] * 10)
    _hook_log(tmp_path / "hook.log", [600.0] * 10)

    decision = _run(tmp_path)

    assert decision.action == "allow"
    assert decision.metrics["dense"]["n"] == 10
    assert decision.metrics["hook"]["p95_ms"] == pytest.approx(600.0)


def test_dense_soft_breach_means_capture_only(tmp_path: Path) -> None:
    _dense_log(tmp_path / "dense.jsonl", [200.0] * 10)

    decision = _run(tmp_path)

    assert decision.action == "capture_only"
    assert any(reason.startswith("dense_soft") for reason in decision.reasons)


def test_dense_hard_breach_means_skip(tmp_path: Path) -> None:
    _dense_log(tmp_path / "dense.jsonl", [350.0] * 10)

    decision = _run(tmp_path)

    assert decision.action == "skip"
    assert any(reason.startswith("dense_hard") for reason in decision.reasons)


def test_hook_soft_and_hard_breaches(tmp_path: Path) -> None:
    _hook_log(tmp_path / "hook.log", [1000.0] * 10)
    assert _run(tmp_path).action == "capture_only"

    _hook_log(tmp_path / "hook.log", [1500.0] * 30)
    hard = _run(tmp_path, guard_log=tmp_path / "guard2.jsonl")
    assert hard.action == "skip"
    assert any(reason.startswith("hook_hard") for reason in hard.reasons)


def test_the_worst_source_wins(tmp_path: Path) -> None:
    _dense_log(tmp_path / "dense.jsonl", [200.0] * 10)  # soft
    _hook_log(tmp_path / "hook.log", [1500.0] * 10)  # hard

    assert _run(tmp_path).action == "skip"


def test_events_older_than_the_window_are_ignored(tmp_path: Path) -> None:
    _dense_log(tmp_path / "dense.jsonl", [900.0] * 10, age_minutes=180)
    _hook_log(tmp_path / "hook.log", [3000.0] * 10, age_minutes=180)

    assert _run(tmp_path).action == "allow"


def test_thresholds_are_overridable_from_the_environment(tmp_path: Path, monkeypatch) -> None:
    _dense_log(tmp_path / "dense.jsonl", [200.0] * 10)
    monkeypatch.setenv("MEMORYMASTER_BEAT_DENSE_SOFT_MS", "250")
    monkeypatch.setenv("MEMORYMASTER_BEAT_DENSE_HARD_MS", "500")

    assert _run(tmp_path).action == "allow"


def test_each_decision_appends_one_structured_json_line(tmp_path: Path) -> None:
    _dense_log(tmp_path / "dense.jsonl", [350.0] * 10)
    _run(tmp_path)
    _run(tmp_path)

    events = _events(tmp_path / "guard.jsonl")
    assert len(events) == 2
    assert {"ts", "action", "reasons", "dense", "hook", "thresholds"} <= set(events[0])
    assert events[0]["ts"] == NOW.isoformat(timespec="seconds")


def test_a_broken_guard_never_blocks(tmp_path: Path, monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(latency_guard, "_dense_metrics", boom)

    decision = _run(tmp_path)

    assert decision.action == "allow"
    assert decision.reasons == ["guard_error:RuntimeError"]


def test_guard_log_defaults_to_the_state_dir_and_hook_log_is_tail_read(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_STATE_DB", str(tmp_path / "state" / "capture-control.db"))
    assert latency_guard.guard_log_path() == tmp_path / "state" / "beat-guard.jsonl"

    big = tmp_path / "hook.log"
    stamp = (NOW - timedelta(minutes=5)).astimezone().isoformat(timespec="seconds")
    with big.open("w", encoding="utf-8") as handle:
        handle.write("x" * (latency_guard.HOOK_LOG_TAIL_BYTES * 3) + "\n")
        for _ in range(10):
            handle.write(f"[{stamp}] hook=recall event=latency_total total_ms=1500.0\n")

    assert _run(tmp_path, hook_log=big).action == "skip"
