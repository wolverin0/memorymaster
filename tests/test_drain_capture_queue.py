"""scripts/drain_capture_queue.py (T-2123): bounded batches that stop by themselves. Every collaborator is injected;
nothing here touches the live ledger, a provider or the network."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.models import CaptureEnvelope, DreamMessage

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("drain_capture_queue", ROOT / "scripts" / "drain_capture_queue.py")
drain_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drain_mod)

OK = {"ok": True, "extracted": 5, "consolidated": 5, "applied": 5, "errors": 0}


class Queue:
    """A queue that shrinks by ``per_batch`` each time a batch runs."""

    def __init__(self, waiting: int, per_batch: int = 100, age_h: float | None = 10.0) -> None:
        self.waiting, self.per_batch, self.age_h, self.batches = waiting, per_batch, age_h, 0

    def state(self) -> dict:
        return {"waiting": self.waiting, "oldest_age_h": self.age_h}

    def batch(self, result: dict | None = None) -> dict:
        self.batches += 1
        self.waiting = max(0, self.waiting - self.per_batch)
        return dict(result or OK)


def run(queue: Queue, **kwargs) -> dict:
    kwargs.setdefault("run_batch", queue.batch)
    return drain_mod.drain(state=queue.state, **kwargs)


def test_it_stops_on_its_own_once_the_queue_is_under_the_probe_threshold() -> None:
    queue = Queue(1350, per_batch=100)
    report = run(queue)
    assert report["stop_reason"] == "under_threshold"
    assert report["batches"] == 4 and queue.waiting == 950
    assert report["before"]["waiting"] == 1350 and report["after"]["waiting"] == 950
    assert report["totals"]["applied"] == 20


def test_an_old_oldest_capture_keeps_it_going_even_when_the_count_is_low() -> None:
    queue = Queue(500, per_batch=0, age_h=200.0)
    report = run(queue, max_batches=3)
    assert report["stop_reason"] == "max_batches" and report["batches"] == 3


def test_an_already_drained_queue_runs_nothing() -> None:
    queue = Queue(10)
    assert run(queue)["batches"] == 0 and queue.batches == 0


def test_an_open_breaker_stops_the_drain_after_that_batch() -> None:
    queue = Queue(3000)
    report = run(queue, run_batch=lambda: queue.batch({**OK, "consolidate_breaker": "open"}))
    assert report["stop_reason"] == "breaker_open" and report["batches"] == 1


@pytest.mark.parametrize("key", ["deferred_extract_budget", "deferred_consolidate_budget"])
def test_a_daily_cap_deferral_stops_the_drain(key: str) -> None:
    queue = Queue(3000)
    report = run(queue, run_batch=lambda: queue.batch({**OK, key: 3}))
    assert report["stop_reason"] == "daily_budget" and report["batches"] == 1


def test_a_batch_that_did_nothing_stops_the_drain() -> None:
    queue = Queue(3000)
    report = run(queue, run_batch=lambda: queue.batch({"ok": True, "extracted": 0, "consolidated": 0, "applied": 0, "errors": 0}))
    assert report["stop_reason"] == "no_progress"


def test_a_batch_that_loses_more_to_errors_than_it_finishes_stops_the_drain() -> None:
    queue = Queue(3000)
    report = run(queue, run_batch=lambda: queue.batch({"ok": True, "extracted": 1, "consolidated": 0, "applied": 0, "errors": 6}))
    assert report["stop_reason"] == "run_failed"


def test_a_raising_batch_ends_the_drain_with_its_reason_instead_of_looping() -> None:
    def boom() -> dict:
        raise RuntimeError("provider down")

    report = run(Queue(3000), run_batch=boom)
    assert report["stop_reason"] == "run_failed:RuntimeError" and report["batches"] == 0


def test_another_worker_holding_the_lease_stops_the_drain() -> None:
    queue = Queue(3000)
    report = run(queue, run_batch=lambda: queue.batch({"ok": False, "reason": "worker_busy"}))
    assert report["stop_reason"] == "worker_busy"


def test_the_latency_guard_pauses_the_drain_before_any_batch() -> None:
    queue = Queue(3000)
    report = run(queue, guard=lambda: "skip")
    assert report["stop_reason"] == "latency_guard_skip" and queue.batches == 0
    assert run(Queue(3000), guard=lambda: "capture_only", max_batches=1)["batches"] == 1  # only a hard skip pauses it


def test_the_wall_clock_bound_stops_the_drain() -> None:
    ticks = iter(range(0, 10_000, 600))  # each clock() read advances ten minutes
    report = run(Queue(3000), clock=lambda: float(next(ticks)), max_minutes=25)
    assert report["stop_reason"] == "max_minutes"


def test_the_last_batch_that_empties_the_queue_reports_under_threshold_not_the_batch_stop() -> None:
    queue = Queue(1010, per_batch=100)
    report = run(queue, run_batch=lambda: queue.batch({**OK, "deferred_extract_budget": 1}))
    assert report["stop_reason"] == "under_threshold"


def test_queue_state_uses_the_ledgers_drainable_definition(tmp_path: Path) -> None:
    now = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
    ledger = DreamLedger(tmp_path / "capture.db")

    def envelope(session: str, turns: int, days_idle: int) -> CaptureEnvelope:
        stamp = (now - timedelta(days=days_idle)).isoformat()
        return CaptureEnvelope(
            provider="codex", session_hash=session, scope="project:test", captured_at=stamp, last_activity_at=stamp,
            messages=tuple(DreamMessage(f"m{i}", "user", f"turn {i}", stamp) for i in range(turns)),
            cursor_start=0, cursor_end=100, content_hash=session,
        )

    ledger.enqueue(envelope("one-turn", 1, 30))  # never eligible: not drainable
    ledger.enqueue(envelope("old", 3, 8))
    ledger.enqueue(envelope("new", 2, 1))
    state = drain_mod.queue_state(ledger, now)
    assert state["waiting"] == 2 and state["oldest_age_h"] == pytest.approx(192.0, abs=0.1)


def test_caps_are_only_ever_raised_for_this_process(monkeypatch) -> None:
    name = "MEMORYMASTER_DREAM_MAX_EXTRACT_CALLS_DAILY"
    monkeypatch.setenv(name, "200")
    drain_mod._raise_cap(name, 700)
    assert __import__("os").environ[name] == "700"
    drain_mod._raise_cap(name, 100)  # a lower ask never lowers an operator's cap
    assert __import__("os").environ[name] == "700"
    monkeypatch.delenv(name)
    drain_mod._raise_cap(name, 700)
    assert __import__("os").environ[name] == "700"
    tokens = "MEMORYMASTER_DREAM_MAX_INPUT_TOKENS_DAILY"
    monkeypatch.setenv(tokens, "0")  # 0 means the token cap is disabled: stays disabled
    drain_mod._raise_cap(tokens, 8_000_000)
    assert __import__("os").environ[tokens] == "0"


def test_plan_is_read_only_and_calls_no_provider(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_STATE_DB", str(tmp_path / "capture-control.db"))
    for name in ("MEMORYMASTER_DREAM_EXTRACT_PROVIDER", "MEMORYMASTER_DREAM_EXTRACT_MODEL",
                 "MEMORYMASTER_DREAM_CONSOLIDATE_MODEL", "MEMORYMASTER_DREAM_MAX_EXTRACT_CALLS_DAILY",
                 "MEMORYMASTER_DREAM_MAX_INPUT_TOKENS_DAILY"):
        monkeypatch.setenv(name, "x" if name.endswith(("PROVIDER", "MODEL")) else "1")  # main() rewrites them; teardown restores
    called = []
    monkeypatch.setattr("memorymaster.dreaming.worker.run_dream", lambda *a, **k: called.append(1))
    assert drain_mod.main(["--db", str(tmp_path / "x.db"), "--workspace", str(tmp_path), "--plan", "--no-guard"]) == 0
    out = capsys.readouterr().out
    assert '"waiting": 0' in out and '"input_tokens_daily"' in out and not called
