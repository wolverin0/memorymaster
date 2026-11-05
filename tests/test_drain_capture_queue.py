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


def test_a_hung_call_breaker_is_soft_and_only_two_in_a_row_stop_the_drain() -> None:
    """T-2123: `TimeoutExpired` means one hung call, not a dead provider: retry the next batch."""
    queue = Queue(3000, per_batch=0, age_h=200.0)
    hung = {**OK, "errors": 1, "consolidate_breaker": "TimeoutExpired"}
    report = run(queue, run_batch=lambda: queue.batch(hung))
    assert report["stop_reason"] == "breaker_open" and report["batches"] == 2


def test_a_healthy_batch_resets_the_soft_breaker_streak() -> None:
    queue = Queue(3000, per_batch=0, age_h=200.0)
    results = iter([
        {**OK, "consolidate_breaker": "TimeoutExpired"}, OK,
        {**OK, "consolidate_breaker": "TimeoutExpired"}, OK, {**OK, "consolidate_breaker": "open"},
    ])
    report = run(queue, run_batch=lambda: queue.batch(next(results)))
    assert report["stop_reason"] == "breaker_open" and report["batches"] == 5


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


def test_a_breaker_or_failure_outranks_a_daily_cap_and_an_empty_queue_but_not_the_goal() -> None:
    """Precedence of a finished batch's stop reason: worker_busy > breaker_open > run_failed > daily_budget > no_progress.
    A cap deferral means 'healthy, rerun tomorrow', so it must never hide a breaker or a failing batch."""
    both = {**OK, "consolidate_breaker": "open", "deferred_consolidate_budget": 2}
    queue = Queue(3000)
    assert run(queue, run_batch=lambda: queue.batch(both))["stop_reason"] == "breaker_open"
    failing = {"ok": True, "extracted": 1, "consolidated": 0, "applied": 0, "errors": 6, "deferred_extract_budget": 2}
    queue = Queue(3000)
    assert run(queue, run_batch=lambda: queue.batch(failing))["stop_reason"] == "run_failed"
    busy = {"ok": False, "reason": "worker_busy", "consolidate_breaker": "open"}
    queue = Queue(3000)
    assert run(queue, run_batch=lambda: queue.batch(busy))["stop_reason"] == "worker_busy"
    idle = {"ok": True, "extracted": 0, "consolidated": 0, "applied": 0, "errors": 0, "deferred_extract_budget": 2}
    queue = Queue(3000)
    assert run(queue, run_batch=lambda: queue.batch(idle))["stop_reason"] == "daily_budget"


def test_reaching_the_goal_does_not_hide_a_breaker_that_opened_in_the_last_batch() -> None:
    queue = Queue(1050, per_batch=100)
    report = run(queue, run_batch=lambda: queue.batch({**OK, "consolidate_breaker": "open"}))
    assert report["stop_reason"] == "breaker_open" and report["after"]["waiting"] == 950


def test_errors_are_weighed_against_captures_finished_not_against_stage_counts_summed() -> None:
    """One capture that is extracted, consolidated and applied is progress 1, not 3: 6 errors against 5 captures fail."""
    queue = Queue(3000)
    six = {"ok": True, "extracted": 5, "consolidated": 5, "applied": 5, "errors": 6}
    assert run(queue, run_batch=lambda: queue.batch(six))["stop_reason"] == "run_failed"
    queue = Queue(3000)
    five = {**six, "errors": 5}
    assert run(queue, run_batch=lambda: queue.batch(five), max_batches=2)["stop_reason"] == "max_batches"


def _drainable(ledger: DreamLedger) -> None:
    stamp = (datetime(2026, 11, 3, tzinfo=timezone.utc) - timedelta(days=1)).isoformat()
    ledger.enqueue(CaptureEnvelope(
        provider="codex", session_hash="s", scope="project:test", captured_at=stamp, last_activity_at=stamp,
        messages=tuple(DreamMessage(f"m{i}", "user", f"turn {i}", stamp) for i in range(2)),
        cursor_start=0, cursor_end=10, content_hash="h",
    ))


def test_raised_caps_reach_the_batch_config_without_touching_the_environment(tmp_path: Path, monkeypatch) -> None:
    """The caps ride in DreamConfig; the provider contract is set for the batches and restored afterwards, so
    nothing leaks into the parent shell, other code in the same process, or the next process."""
    import os

    state_db = tmp_path / "capture-control.db"
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_STATE_DB", str(state_db))
    monkeypatch.setenv("MEMORYMASTER_DREAM_MAX_EXTRACT_CALLS_DAILY", "200")
    monkeypatch.setenv("MEMORYMASTER_DREAM_MAX_INPUT_TOKENS_DAILY", "2000000")
    monkeypatch.setenv("MEMORYMASTER_DREAM_EXTRACT_PROVIDER", "claude")  # the user's own value must come back
    monkeypatch.delenv("MEMORYMASTER_DREAM_EXTRACT_MODEL", raising=False)
    _drainable(DreamLedger(state_db))
    seen: dict = {}

    def fake_run_dream(db, workspace, *, apply_candidates, config):
        seen["config"] = config
        seen["provider_env"] = os.environ.get("MEMORYMASTER_DREAM_EXTRACT_PROVIDER")
        seen["apply"] = apply_candidates
        return {"ok": True, "extracted": 0, "consolidated": 0, "applied": 0, "errors": 0}

    monkeypatch.setattr("memorymaster.dreaming.worker.run_dream", fake_run_dream)
    before = dict(os.environ)

    code = drain_mod.main(["--db", "x.db", "--workspace", str(tmp_path), "--stop-below", "0", "--no-guard"])

    assert code == 2  # stopped on no_progress, not on the goal
    assert seen["config"].max_input_tokens_daily == 8_000_000 and seen["config"].max_extract_calls_daily == 700
    assert seen["provider_env"] == "gemini" and seen["apply"] is True
    assert dict(os.environ) == before, "the drain left the environment as it found it"


def test_a_lower_ask_never_lowers_a_cap_and_a_disabled_token_cap_stays_disabled() -> None:
    from memorymaster.dreaming.worker import DreamConfig

    config = DreamConfig(max_extract_calls_daily=900, max_input_tokens_daily=0)
    raised = drain_mod._with_caps(config, extract_calls_daily=700, max_input_tokens_daily=8_000_000)
    assert raised.max_extract_calls_daily == 900 and raised.max_input_tokens_daily == 0
    low = DreamConfig(max_extract_calls_daily=200, max_input_tokens_daily=2_000_000)
    raised = drain_mod._with_caps(low, extract_calls_daily=700, max_input_tokens_daily=8_000_000)
    assert (raised.max_extract_calls_daily, raised.max_input_tokens_daily) == (700, 8_000_000)
    assert (low.max_extract_calls_daily, low.max_input_tokens_daily) == (200, 2_000_000)  # the source config is untouched


def test_plan_is_read_only_and_calls_no_provider(tmp_path: Path, monkeypatch, capsys) -> None:
    import os

    monkeypatch.setenv("MEMORYMASTER_CAPTURE_STATE_DB", str(tmp_path / "capture-control.db"))
    called = []
    monkeypatch.setattr("memorymaster.dreaming.worker.run_dream", lambda *a, **k: called.append(1))
    before = dict(os.environ)
    assert drain_mod.main(["--db", str(tmp_path / "x.db"), "--workspace", str(tmp_path), "--plan", "--no-guard"]) == 0
    out = capsys.readouterr().out
    assert '"waiting": 0' in out and '"input_tokens_daily": 8000000' in out and not called
    assert dict(os.environ) == before
