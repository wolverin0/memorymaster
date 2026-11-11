"""D-11: a dream run is bounded by a wall-clock budget, abandons a hung call, keeps its lease across a long
call, logs every provider call, and the scheduled budgets fit the Windows task limits. No real provider and
no real sleeps beyond millisecond polling: provider time is a fake clock the stubs advance."""

from __future__ import annotations

import json
import logging
import re
import runpy
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memorymaster.core.structured_log import _FIELDS_ATTR
from memorymaster.dreaming import worker as worker_module
from memorymaster.dreaming.ledger import LANE_BEAT, LANE_DREAM, DreamLedger
from memorymaster.dreaming.worker import DreamConfig, DreamWorker
from memorymaster.knowledge.graph_observation_engine import ObservationBudgetDeferred
from memorymaster.surfaces import scheduled_task

H = runpy.run_path("tests/test_dreaming_worker.py")
NOW = H["NOW"]
EXTRACTOR = runpy.run_path("tests/test_beat_worker_cap.py")["_PerCaptureExtractor"]
MANIFEST = Path(__file__).resolve().parents[1] / "ops" / "windows-tasks" / "manifest.json"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class TimedExtractor(EXTRACTOR):
    """Each call costs ``cost`` seconds of fake time; ``timeout`` is its declared hard timeout."""

    timeout = 100

    def __init__(self, clock: Clock, cost: float = 100.0) -> None:
        super().__init__()
        self.clock, self.cost, self.calls = clock, cost, 0

    def extract(self, messages, *, scope, capture_hash):
        self.calls += 1
        self.clock.t += self.cost
        return super().extract(messages, scope=scope, capture_hash=capture_hash)


class TimedConsolidator(H["_Consolidator"]):
    timeout = 50

    def __init__(self, clock: Clock, cost: float = 0.0) -> None:
        super().__init__()
        self.clock, self.cost = clock, cost

    def consolidate(self, candidates, current_claims, *, scope):
        self.clock.t += self.cost
        return super().consolidate(candidates, current_claims, scope=scope)


def _queue(ledger: DreamLedger, count: int) -> list[int]:
    return [H["_capture"](ledger, session_hash=f"s{n}", extra_text=f" {n}") for n in range(count)]


def _worker(tmp_path: Path, clock: Clock, extractor, consolidator, **config) -> tuple[DreamWorker, DreamLedger]:
    ledger = DreamLedger(tmp_path / "capture.db")
    cfg = replace(DreamConfig(), idle_minutes=0, jev_triage_enabled=False, **config)
    return DreamWorker(ledger, H["_service"](tmp_path), extractor, consolidator, config=cfg,
                       now=lambda: NOW, clock=clock), ledger


def _run_status(ledger: DreamLedger, run_id: str) -> str:
    with ledger._connect() as conn:
        return conn.execute("SELECT status FROM dream_runs WHERE run_id=?", (run_id,)).fetchone()[0]


def test_budget_stop_closes_the_run_and_the_rest_resumes_next_run(tmp_path: Path) -> None:
    clock = Clock()
    extractor = TimedExtractor(clock)
    worker, ledger = _worker(tmp_path, clock, extractor, TimedConsolidator(clock), run_budget_seconds=400, max_sessions=6)
    ids = _queue(ledger, 5)

    first = worker.run(apply_candidates=False)

    # 400 s: calls start with 400, 300, 200 s left (a call needs 100 + 5 grace + 10 margin); the 4th does not fit.
    assert extractor.calls == 3 and first["extracted"] == 3 and first["budget_stop"] == "extract"
    assert first["ok"] is True and _run_status(ledger, first["run_id"]) == "ok"  # closed, never abandoned
    assert sum(ledger.get_capture(i)["state"] == "captured" for i in ids) == 2
    assert scheduled_task.dream_outcome(first) == ("budget_stop", 0)

    clock.t = 0
    second = worker.run(apply_candidates=False)
    assert second["extracted"] == 2 and extractor.calls == 5  # nothing was lost or redone
    assert all(ledger.get_capture(i)["state"] in {"consolidated", "extracted"} for i in ids)


def test_a_call_that_would_outlast_the_budget_is_never_started(tmp_path: Path) -> None:
    clock = Clock()
    extractor = TimedExtractor(clock)
    worker, ledger = _worker(tmp_path, clock, extractor, TimedConsolidator(clock), run_budget_seconds=114)
    _queue(ledger, 2)

    summary = worker.run(apply_candidates=False)

    assert extractor.calls == 0 and summary["budget_stop"] == "extract" and summary["errors"] == 0


def test_without_a_budget_nothing_changes(tmp_path: Path) -> None:
    clock = Clock()
    extractor = TimedExtractor(clock, cost=1e6)
    worker, ledger = _worker(tmp_path, clock, extractor, TimedConsolidator(clock), max_sessions=6)
    _queue(ledger, 3)

    summary = worker.run(apply_candidates=False)

    assert extractor.calls == 3 and summary["budget_stop"] is None


class HangingExtractor(EXTRACTOR):
    """The first capture answers, every later call hangs until released (a real hang, short real timeout)."""

    timeout = 0.05

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.calls = 0

    def extract(self, messages, *, scope, capture_hash):
        self.calls += 1
        if self.calls > 1:
            self.release.wait(10)
        return super().extract(messages, scope=scope, capture_hash=capture_hash)


def test_a_hung_call_is_abandoned_counted_and_sent_to_the_slow_lane(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "WATCHDOG_GRACE_SECONDS", 0.0)
    extractor = HangingExtractor()
    worker, ledger = _worker(tmp_path, Clock(), extractor, TimedConsolidator(Clock()), max_sessions=6,
                             lease_ttl_seconds=900)
    ids = _queue(ledger, 2)
    try:
        started = time.monotonic()
        summary = worker.run(apply_candidates=False)
        assert time.monotonic() - started < 5  # abandoned, not waited for
    finally:
        extractor.release.set()

    answered, hung = (ledger.get_capture(i) for i in ids)
    assert summary["abandoned_calls"] == 1 and summary["errors"] == 1 and summary["ok"] is True
    # The provider answered another call, so the hang is charged to the capture: retryable, one error.
    assert hung["state"] == "retryable" and hung["error_count"] == 1 and answered["state"] != "retryable"
    now = datetime.now(timezone.utc) + timedelta(days=2)
    assert hung["id"] not in {r["id"] for r in ledger.eligible(idle_minutes=0, max_sessions=9, now=now, lane=LANE_BEAT)}
    due = {r["id"] for r in ledger.eligible(idle_minutes=0, max_sessions=9, now=now, lane=LANE_DREAM)}
    assert hung["id"] in due  # the 6-hourly run takes it once the backoff elapsed


def test_a_timeout_with_no_answer_in_the_run_is_not_charged_to_the_capture(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "WATCHDOG_GRACE_SECONDS", 0.0)

    class AlwaysHangs(HangingExtractor):
        def extract(self, messages, *, scope, capture_hash):
            self.release.wait(10)

    extractor = AlwaysHangs()
    worker, ledger = _worker(tmp_path, Clock(), extractor, TimedConsolidator(Clock()), max_sessions=6)
    ids = _queue(ledger, 4)
    try:
        summary = worker.run(apply_candidates=False)
    finally:
        extractor.release.set()

    assert summary["abandoned_calls"] == worker_module.MAX_ABANDONED_CALLS  # the run stops calling a dead provider
    assert ledger.get_capture(ids[0])["error_count"] == 0  # an outage must not walk captures toward quarantine


class LeaseProbe(EXTRACTOR):
    """A long call: fake wall time jumps past the lease TTL while the call is in flight; a rival worker tries
    the lease each time. With the heartbeat the rival never gets it."""

    timeout = 600

    def __init__(self, ledger: DreamLedger, fake_now, renews: list[int], rival: list[bool], beats: int, step: float,
                 wait: bool) -> None:
        super().__init__()
        self.ledger, self.fake_now, self.renews, self.rival, self.beats = ledger, fake_now, renews, rival, beats
        self.step, self.wait = step, wait

    def extract(self, messages, *, scope, capture_hash):
        for _ in range(self.beats):
            seen = len(self.renews)
            self.fake_now.advance(self.step)  # ttl is 1 s: one missed heartbeat and the lease is expired
            deadline = time.monotonic() + (5 if self.wait else 0)
            while len(self.renews) == seen and time.monotonic() < deadline:
                time.sleep(0.002)
            self.rival.append(self.ledger.acquire_lease("dream-worker", "rival", 1, now=self.fake_now()))
        return super().extract(messages, scope=scope, capture_hash=capture_hash)


class FakeNow:
    def __init__(self) -> None:
        self.base, self.offset = NOW, 0.0

    def advance(self, seconds: float) -> None:
        self.offset += seconds

    def __call__(self) -> datetime:
        return self.base + timedelta(seconds=self.offset)


def _lease_run(tmp_path: Path, monkeypatch, *, heartbeat: float, beats: int, step: float, wait: bool):
    monkeypatch.setattr(worker_module, "_HEARTBEAT_FLOOR_S", heartbeat)
    fake_now = FakeNow()
    ledger = DreamLedger(tmp_path / "capture.db")
    renews: list[int] = []
    original = ledger.renew_lease

    def counting(*args, **kwargs):
        renews.append(1)
        return original(*args, **kwargs)

    ledger.renew_lease = counting  # type: ignore[method-assign]
    rival: list[bool] = []
    extractor = LeaseProbe(ledger, fake_now, renews, rival, beats, step, wait)
    cfg = replace(DreamConfig(), idle_minutes=0, jev_triage_enabled=False, lease_ttl_seconds=0)
    worker = DreamWorker(ledger, H["_service"](tmp_path), extractor, TimedConsolidator(Clock()), config=cfg, now=fake_now)
    ids = _queue(ledger, 1)
    return worker, ledger, ids, rival


def test_the_lease_is_kept_across_a_call_longer_than_its_ttl(tmp_path: Path, monkeypatch) -> None:
    worker, ledger, ids, rival = _lease_run(tmp_path, monkeypatch, heartbeat=0.01, beats=3, step=0.9, wait=True)

    summary = worker.run(apply_candidates=False)

    assert rival == [False, False, False]  # the rival never saw an expired lease
    assert summary["ok"] is True and summary["errors"] == 0 and "reason" not in summary
    assert ledger.get_capture(ids[0])["state"] in {"extracted", "consolidated"}


def test_a_lost_lease_stops_the_run_without_writing(tmp_path: Path, monkeypatch) -> None:
    worker, ledger, ids, rival = _lease_run(tmp_path, monkeypatch, heartbeat=1000, beats=1, step=2.0, wait=False)  # no heartbeat

    summary = worker.run(apply_candidates=False)

    assert rival == [True]  # the TTL ran out and the rival took over
    assert summary["ok"] is False and summary["reason"] == "lease_lost"
    assert ledger.get_capture(ids[0])["state"] == "captured"  # the stale run wrote nothing
    assert scheduled_task.dream_outcome(summary) == ("lease_lost", 1)


def test_every_provider_call_logs_a_start_and_an_end_with_ids_and_sizes_only(tmp_path: Path, caplog) -> None:
    clock = Clock()
    worker, ledger = _worker(tmp_path, clock, TimedExtractor(clock, cost=1), TimedConsolidator(clock), max_sessions=6)
    ids = _queue(ledger, 1)

    with caplog.at_level(logging.INFO, logger="memorymaster.jobs"):
        worker.run(apply_candidates=False)

    events = [(r.getMessage(), getattr(r, _FIELDS_ATTR, {})) for r in caplog.records if r.name == "memorymaster.jobs"]
    starts = [f for name, f in events if name == "dream_call_start"]
    ends = [f for name, f in events if name == "dream_call_end"]
    assert [f["stage"] for f in starts][0] == "extract" and len(starts) >= 2 and len(ends) == len(starts)
    assert {f["stage"] for f in starts} == {"extract", "consolidate"}
    assert starts[0]["capture_id"] == ids[0] and starts[0]["capture_ids"] == ids and starts[0]["bytes"] > 0
    assert starts[0]["provider"] == "google" and starts[0]["timeout_s"] == 100
    assert all(f["outcome"] == "ok" and f["duration_ms"] >= 0 for f in ends)
    assert "blue" not in json.dumps([f for _, f in events]).lower()  # never content


def test_a_failed_call_logs_its_outcome(tmp_path: Path, monkeypatch, caplog) -> None:
    class Boom(EXTRACTOR):
        def extract(self, messages, *, scope, capture_hash):
            raise ValueError("bad output")

    worker, ledger = _worker(tmp_path, Clock(), Boom(), TimedConsolidator(Clock()))
    _queue(ledger, 1)
    with caplog.at_level(logging.INFO, logger="memorymaster.jobs"):
        worker.run(apply_candidates=False)

    ends = [getattr(r, _FIELDS_ATTR) for r in caplog.records if r.getMessage() == "dream_call_end"]
    assert ends[0]["outcome"] == "error" and ends[0]["error_type"] == "ValueError"


# --- the scheduled budgets fit the task limits --------------------------------------------------------------

def _seconds(duration: str) -> int:
    match = re.fullmatch(r"P?(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    assert match, duration
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _task(name: str) -> dict:
    return next(task for task in json.loads(MANIFEST.read_text(encoding="utf-8"))["tasks"] if task["name"] == name)


def test_the_beat_budget_leaves_room_under_the_task_execution_limit() -> None:
    limit = _seconds(_task("MemoryMaster-Beat")["settings"]["execution_time_limit"])
    assert scheduled_task.BEAT_RUN_BUDGET_SECONDS <= limit - 5 * 60


def test_the_dreaming_budget_ends_before_the_next_six_hourly_slot() -> None:
    task = _task("MemoryMaster-Dreaming")
    interval = _seconds(task["triggers"][0]["repetition"]["interval"])
    limit = _seconds(task["settings"].get("execution_time_limit", "PT72H"))
    assert scheduled_task.DREAM_RUN_BUDGET_SECONDS <= min(interval, limit) - 3600


def test_a_hung_consolidation_after_an_answer_charges_the_capture_and_moves_it_to_the_slow_lane(tmp_path: Path) -> None:
    import subprocess
    from dataclasses import replace as _replace

    class HangsOnSecond(H["_Consolidator"]):
        calls = 0

        def consolidate(self, candidates, current_claims, *, scope):
            type(self).calls += 1
            if type(self).calls == 2:
                raise subprocess.TimeoutExpired("agy", 300)
            return super().consolidate(candidates, current_claims, scope=scope)

    ledger = DreamLedger(tmp_path / "capture.db")
    ids = [H["_extracted_capture"](ledger, session_hash=f"s{i}", candidate_id=f"c{i}") for i in range(3)]
    cfg = _replace(DreamConfig(), max_consolidate_candidates=1, jev_triage_enabled=False)
    summary = DreamWorker(ledger, H["_service"](tmp_path), H["_NoCall"](), HangsOnSecond(), config=cfg,
                          now=lambda: NOW).run(apply_candidates=False)

    hung = [i for i in ids if ledger.get_capture(i)["state"] == "extracted"]
    assert summary["consolidate_hung_calls"] == 1 and len(hung) == 1
    assert ledger.get_capture(hung[0])["error_count"] == 1  # charged: backoff and quarantine now apply
    far = datetime.now(timezone.utc) + timedelta(days=2)
    assert hung[0] not in {r["id"] for r in ledger.eligible(idle_minutes=0, max_sessions=9, now=far, lane=LANE_BEAT)}
    assert hung[0] in {r["id"] for r in ledger.eligible(idle_minutes=0, max_sessions=9, now=far, lane=LANE_DREAM)}


# --- the Claude CLI wraps its own timeout (ProviderCallError from TimeoutExpired) -----------------------------

def _wrapped_timeout():
    import subprocess

    from memorymaster.dreaming.providers import ProviderCallError

    try:
        raise subprocess.TimeoutExpired("claude", 180)
    except subprocess.TimeoutExpired as exc:
        wrapper = ProviderCallError("Claude CLI extraction timed out")
        wrapper.outcome = "timeout"
        wrapper.__cause__ = exc
        return wrapper


def test_a_wrapped_timeout_is_charged_when_the_provider_answered_and_stops_the_run_after_two(tmp_path: Path) -> None:
    class AnswersOnceThenTimesOut(EXTRACTOR):
        calls = 0

        def extract(self, messages, *, scope, capture_hash):
            type(self).calls += 1
            if type(self).calls > 1:
                raise _wrapped_timeout()
            return super().extract(messages, scope=scope, capture_hash=capture_hash)

    worker, ledger = _worker(tmp_path, Clock(), AnswersOnceThenTimesOut(), TimedConsolidator(Clock()), max_sessions=6)
    ids = _queue(ledger, 5)

    summary = worker.run(apply_candidates=False)

    assert AnswersOnceThenTimesOut.calls == 3  # one answer + two timeouts, then the run stops calling
    assert summary["abandoned_calls"] == 2
    charged = [ledger.get_capture(i) for i in ids if ledger.get_capture(i)["state"] == "retryable"]
    assert len(charged) == 2 and all(row["error_count"] == 1 for row in charged)  # slow lane, backoff, quarantine


def test_a_wrapped_timeout_with_no_answer_is_not_charged(tmp_path: Path) -> None:
    class AlwaysTimesOut(EXTRACTOR):
        def extract(self, messages, *, scope, capture_hash):
            raise _wrapped_timeout()

    worker, ledger = _worker(tmp_path, Clock(), AlwaysTimesOut(), TimedConsolidator(Clock()), max_sessions=6)
    ids = _queue(ledger, 4)

    summary = worker.run(apply_candidates=False)

    assert summary["abandoned_calls"] == 2  # the dead provider is left alone after two
    assert [ledger.get_capture(i)["error_count"] for i in ids] == [0, 0, 0, 0]


# --- graph observations are under the same budget ------------------------------------------------------------

def test_an_observation_call_is_skipped_when_the_budget_cannot_hold_it_and_guarded_otherwise(tmp_path: Path, monkeypatch, caplog) -> None:
    import pytest

    clock = Clock()
    worker, _ = _worker(tmp_path, clock, TimedExtractor(clock), TimedConsolidator(clock), run_budget_seconds=100)
    worker._deadline = clock() + 100
    with pytest.raises(ObservationBudgetDeferred):
        worker._observation_llm("system", "prompt")
    assert worker._summary["graph_observation_calls_skipped"] == 1

    monkeypatch.setattr("memorymaster.core.llm_provider.call_llm", lambda system, prompt: '{"decision":"no_signal"}')
    worker._deadline = clock() + 10_000
    with caplog.at_level(logging.INFO, logger="memorymaster.jobs"):
        assert worker._observation_llm("system", "prompt") == '{"decision":"no_signal"}'
    stages = [getattr(r, _FIELDS_ATTR)["stage"] for r in caplog.records if r.getMessage() == "dream_call_start"]
    assert stages == ["observe"]


# --- the beat drains a backlog even when a third of its extractions fail --------------------------------------

def _simulate(tmp_path: Path, monkeypatch, *, exile_retryable: bool = False, failure_rate: float = 0.35,
              kind: str = "transient", dream_runs: bool = False, exile_charged: bool = False) -> tuple[int, int]:
    """48 simulated hours of 15-minute beat ticks (2 arrivals, 6 slots each); returns (initial, final) unfinished."""
    import random

    from memorymaster.dreaming import ledger as ledger_module
    from memorymaster.dreaming.models import CaptureEnvelope, DreamCandidate, DreamMessage, ExtractionResult
    from memorymaster.dreaming.providers import ProviderCallError

    tmp_path.mkdir(parents=True, exist_ok=True)
    sim = {"now": datetime(2026, 7, 21, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(ledger_module, "_utc_now", lambda: sim["now"])
    if exile_retryable:  # the policy of ea294b75: any retryable capture leaves the beat
        original = ledger_module._lane_clause

        def exiling(lane, current):
            sql, params = original(lane, current)
            return sql + (" AND c.state != 'retryable'" if lane == LANE_BEAT else ""), params

        monkeypatch.setattr(ledger_module, "_lane_clause", exiling)
    if exile_charged:  # the policy of c945f6da: one counted failure already exiles a capture
        monkeypatch.setattr(ledger_module, "_SLOW", "(c.byte_count >= :slow_bytes OR c.error_count > 0)")
    rng = random.Random(11)

    class Flaky:
        model, provider = "gemini-3.5-flash", "google"

        def extract(self, messages, *, scope, capture_hash):
            if rng.random() < failure_rate:
                if kind == "timeout":
                    raise _wrapped_timeout()
                raise ProviderCallError("provider request failed with HTTP 503", http_status=503)
            tag = capture_hash.replace(":", "-")
            return ExtractionResult((
                DreamCandidate(f"project-{tag}", "The blue interface is selected for this project.", "decision", "interface",
                               "uses", "blue", "project", "m2", "Blue interfaces", 0.8),
            ), H["_usage"]("google", self.model))

    counter = {"n": 0}

    def arrive(ledger: DreamLedger, activity: datetime) -> None:
        counter["n"] += 1
        stamp = activity.isoformat()
        messages = (DreamMessage("m1", "user", "I prefer blue interfaces for daily work.", stamp),
                    DreamMessage("m2", "assistant", f"Blue interfaces will be treated as your preference {counter['n']}", stamp))
        ledger.enqueue(CaptureEnvelope(provider="codex", session_hash=f"s{counter['n']}", scope="project:test",
                                       captured_at=stamp, last_activity_at=stamp, messages=messages, cursor_start=0,
                                       cursor_end=100, content_hash=f"h{counter['n']}"))

    ledger = DreamLedger(tmp_path / "capture.db")
    for n in range(60):
        arrive(ledger, sim["now"] - timedelta(days=2, minutes=n))
    cfg = replace(DreamConfig(), idle_minutes=0, jev_triage_enabled=False, max_sessions=6, oldest_reserve=2,
                  max_consolidate_captures_per_run=6, lane=LANE_BEAT, max_extract_calls_daily=10**6,
                  max_consolidate_calls_daily=10**6, max_input_tokens_daily=10**9)
    worker = DreamWorker(ledger, H["_service"](tmp_path), Flaky(), H["_Consolidator"](), config=cfg, now=lambda: sim["now"])

    def unfinished() -> int:
        with ledger._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM dream_captures WHERE state IN ('captured','retryable','extracted')").fetchone()[0]

    dream_cfg = replace(cfg, max_sessions=20, oldest_reserve=10, lane=LANE_DREAM, slow_slots=500, max_consolidate_captures_per_run=0)
    dreamer = DreamWorker(ledger, H["_service"](tmp_path), Flaky(), H["_Consolidator"](), config=dream_cfg, now=lambda: sim["now"])
    initial = unfinished()
    for tick in range(96 * 2):
        sim["now"] += timedelta(minutes=15)
        for _ in range(2):
            arrive(ledger, sim["now"] - timedelta(minutes=45))
        worker.run(apply_candidates=False)
        if dream_runs and tick % 24 == 23:  # the 6-hourly run
            dreamer.run(apply_candidates=False)
    return initial, unfinished()


def test_the_beat_drains_a_backlog_at_35_percent_extraction_failures(tmp_path: Path, monkeypatch) -> None:
    initial, final = _simulate(tmp_path / "new", monkeypatch, exile_retryable=False)
    assert final < initial  # the backlog shrank over 48 h although a third of the calls failed


def test_exiling_every_retryable_capture_from_the_beat_does_not_drain(tmp_path: Path, monkeypatch) -> None:
    _, with_wait = _simulate(tmp_path / "new", monkeypatch, exile_retryable=False)
    monkeypatch.undo()
    _, exiled = _simulate(tmp_path / "old", monkeypatch, exile_retryable=True)
    print(f"final unfinished after 48 h at 35% failures: short wait={with_wait} exiled={exiled}")
    assert exiled > with_wait


# --- the selection is linear in the queue -----------------------------------------------------------------------

def test_selection_stays_fast_on_a_ten_thousand_row_ledger(tmp_path: Path) -> None:
    import sqlite3

    ledger = DreamLedger(tmp_path / "capture.db")
    first = H["_capture"](ledger, session_hash="seed")
    with sqlite3.connect(ledger.db_path) as conn:
        row = conn.execute("SELECT * FROM dream_captures WHERE id=?", (first,)).fetchone()
        names = [c[1] for c in conn.execute("PRAGMA table_info(dream_captures)")]
        record = dict(zip(names, row))
        cols = [c for c in names if c != "id"]
        for n in range(10_000):  # 800 sessions, chains of 12 unfinished captures
            record.update(capture_key=f"k{n}", session_hash=f"sess{n % 800}", cursor_start=n, cursor_end=n + 1, content_hash=f"c{n}")
            conn.execute(f"INSERT INTO dream_captures ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         [record[c] for c in cols])
    started = time.monotonic()
    rows = ledger.eligible(idle_minutes=0, max_sessions=6, now=NOW + timedelta(days=1), oldest_reserve=2, lane=LANE_BEAT)
    elapsed = time.monotonic() - started
    print(f"eligible on 10k rows: {elapsed:.3f}s")
    assert len(rows) == 6 and elapsed < 3.0


def test_the_beat_drains_when_every_failure_is_counted(tmp_path: Path, monkeypatch) -> None:
    initial, final = _simulate(tmp_path / "new", monkeypatch, kind="timeout", dream_runs=True)
    assert final < initial


def test_exiling_after_one_counted_failure_does_not_drain(tmp_path: Path, monkeypatch) -> None:
    _, second = _simulate(tmp_path / "new", monkeypatch, kind="timeout", dream_runs=True)
    monkeypatch.undo()
    initial, first = _simulate(tmp_path / "old", monkeypatch, kind="timeout", dream_runs=True, exile_charged=True)
    print(f"counted failures, 48 h at 35%: initial={initial} exile-after-2nd={second} exile-after-1st={first}")
    assert first > second
