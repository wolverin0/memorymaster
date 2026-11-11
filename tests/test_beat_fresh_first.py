"""Selection of a run's slots (D-11): the newest captures take most slots, the backlog keeps a fixed minority,
slow captures stay out of the beat lane, and a session's captures are never taken out of order."""

from __future__ import annotations

import runpy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memorymaster.dreaming.ledger import (
    LANE_BEAT,
    LANE_DREAM,
    SLOW_BACKOFF_BASE_HOURS,
    SLOW_CAPTURE_BYTES,
    DreamLedger,
)
from memorymaster.dreaming.models import CaptureEnvelope, DreamMessage
from memorymaster.dreaming.worker import DreamConfig, DreamWorker
from memorymaster.surfaces import scheduled_task

H = runpy.run_path("tests/test_dreaming_worker.py")
NOW = H["NOW"]
EXTRACTOR = runpy.run_path("tests/test_beat_worker_cap.py")["_PerCaptureExtractor"]


def _aged(ledger: DreamLedger, capture_id: int, age_days: int) -> None:
    stamp = (NOW - timedelta(days=age_days) - timedelta(hours=1)).isoformat()
    with ledger._connect() as conn:
        conn.execute("UPDATE dream_captures SET last_activity_at=? WHERE id=?", (stamp, capture_id))


def _queue(ledger: DreamLedger, backlog: int, fresh: int) -> tuple[list[int], list[int]]:
    """`backlog` captures 5 days old, then `fresh` captures of today, each in its own session, by id."""
    old = [H["_capture"](ledger, session_hash=f"old{n}", extra_text=f" {n}") for n in range(backlog)]
    new = [H["_capture"](ledger, session_hash=f"new{n}", extra_text=f" {n}") for n in range(fresh)]
    for capture_id in old:
        _aged(ledger, capture_id, 5)
    for offset, capture_id in enumerate(new):  # strictly increasing activity: new[-1] is the newest
        with ledger._connect() as conn:
            conn.execute("UPDATE dream_captures SET last_activity_at=? WHERE id=?",
                         ((NOW - timedelta(hours=2) + timedelta(minutes=offset)).isoformat(), capture_id))
    return old, new


def _picked(ledger: DreamLedger, slots: int, reserve: int, **kwargs) -> list[int]:
    rows = ledger.eligible(idle_minutes=0, max_sessions=slots, now=NOW, oldest_reserve=reserve, **kwargs)
    return [int(row["id"]) for row in rows]


def test_most_slots_go_to_the_newest_captures_and_a_fixed_minority_to_the_backlog(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    old, new = _queue(ledger, backlog=6, fresh=6)

    picked = _picked(ledger, slots=6, reserve=2)

    assert picked == [new[5], new[4], new[3], new[2], old[0], old[1]]  # 4 newest first, then the 2 oldest


def test_the_selection_is_deterministic(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    _queue(ledger, backlog=6, fresh=6)

    assert _picked(ledger, slots=6, reserve=2) == _picked(ledger, slots=6, reserve=2)


def test_when_fresh_captures_run_out_the_newest_slots_take_the_newest_backlog(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    old, new = _queue(ledger, backlog=4, fresh=1)

    assert _picked(ledger, slots=3, reserve=1) == [new[0], old[3], old[0]]  # 2 newest slots, 1 oldest slot


def test_the_oldest_slot_is_taken_even_when_every_capture_is_recent(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    _, new = _queue(ledger, backlog=0, fresh=5)

    assert _picked(ledger, slots=3, reserve=1) == [new[4], new[3], new[0]]


def test_with_no_reserve_the_order_stays_oldest_first(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    old, _ = _queue(ledger, backlog=4, fresh=4)

    assert _picked(ledger, slots=3, reserve=0) == old[:3]
    assert DreamConfig().oldest_reserve == 0 and DreamConfig.from_env().oldest_reserve == 0


def test_the_schedules_split_their_slots_between_newest_and_backlog() -> None:
    assert scheduled_task.BEAT_MAX_SESSIONS == 6
    assert scheduled_task.BEAT_MAX_CONSOLIDATE_CAPTURES == 6
    assert (scheduled_task.BEAT_MAX_SESSIONS - scheduled_task.BEAT_OLDEST_RESERVE,
            scheduled_task.BEAT_OLDEST_RESERVE) == (4, 2)
    assert scheduled_task.DREAM_OLDEST_RESERVE == 10  # of the 20 sessions of the 6-hourly run


def test_a_three_slot_run_consolidates_three_captures(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    old, new = _queue(ledger, backlog=3, fresh=3)
    config = replace(DreamConfig(), max_sessions=3, max_consolidate_captures_per_run=3, oldest_reserve=1, idle_minutes=0)
    worker = DreamWorker(ledger, H["_service"](tmp_path), EXTRACTOR(), H["_Consolidator"](), config=config, now=lambda: NOW)

    summary = worker.run(apply_candidates=False)

    assert summary["consolidated"] == 3 and summary["deferred_consolidate_budget"] == 0 and summary["errors"] == 0
    states = {capture_id: ledger.get_capture(capture_id)["state"] for capture_id in old + new}
    assert [states[i] for i in (new[2], new[1], old[0])] == ["consolidated"] * 3  # newest first, backlog got its slot
    assert [states[i] for i in (new[0], old[1], old[2])] == ["captured"] * 3  # the rest wait for the next tick


# --- slow lane -------------------------------------------------------------------------------------------

def _set(ledger: DreamLedger, capture_id: int, **columns) -> None:
    assignments = ", ".join(f"{name}=?" for name in columns)
    with ledger._connect() as conn:
        conn.execute(f"UPDATE dream_captures SET {assignments} WHERE id=?", (*columns.values(), capture_id))


def test_large_and_failed_captures_never_take_a_beat_slot(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    small, large, failed, retry = (H["_capture"](ledger, session_hash=f"s{n}") for n in range(4))
    _set(ledger, large, byte_count=SLOW_CAPTURE_BYTES)
    _set(ledger, failed, error_count=1)
    _set(ledger, retry, state="retryable")

    assert _picked(ledger, 10, 2, lane=LANE_BEAT) == [small]
    assert sorted(_picked(ledger, 10, 2)) == sorted([small, large, failed, retry])  # no lane rule: unchanged


def test_the_dream_run_takes_a_large_capture_and_respects_the_failure_backoff(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    large, failed = (H["_capture"](ledger, session_hash=f"s{n}") for n in range(2))
    _set(ledger, large, byte_count=SLOW_CAPTURE_BYTES)
    _set(ledger, failed, state="retryable", error_count=2)  # 2nd failure: wait 2 x base
    wait = SLOW_BACKOFF_BASE_HOURS * 2
    failed_at = datetime(2026, 7, 21, 13, tzinfo=timezone.utc)
    _set(ledger, failed, updated_at=failed_at.isoformat())
    _set(ledger, large, updated_at=(failed_at - timedelta(days=1)).isoformat())

    def picked(at: datetime) -> list[int]:
        rows = ledger.eligible(idle_minutes=0, max_sessions=10, now=at, oldest_reserve=2, lane=LANE_DREAM)
        return sorted(int(row["id"]) for row in rows)

    assert picked(failed_at + timedelta(hours=wait - 1)) == [large]  # a large capture has no wait of its own
    assert picked(failed_at + timedelta(hours=wait + 1)) == sorted([large, failed])


def test_the_backoff_doubles_with_every_failure_and_stops_at_the_cap(tmp_path: Path) -> None:
    from memorymaster.dreaming.ledger import SLOW_BACKOFF_MAX_HOURS, _backoff_hours

    hours = _backoff_hours()
    assert hours[0] == 0 and hours[1:4] == [5, 10, 20] and max(hours) == SLOW_BACKOFF_MAX_HOURS


# --- one session stays in order ----------------------------------------------------------------------------

def _second_capture(ledger: DreamLedger, session_hash: str) -> int:
    messages = (
        DreamMessage("m1", "user", "I prefer blue interfaces for daily work.", (NOW - timedelta(minutes=50)).isoformat()),
        DreamMessage("m2", "assistant", "Blue interfaces will be treated as your preference 2", (NOW - timedelta(minutes=49)).isoformat()),
    )
    return ledger.enqueue(CaptureEnvelope(
        provider="codex", session_hash=session_hash, scope="project:test",
        captured_at=NOW.isoformat(), last_activity_at=messages[-1].timestamp,
        messages=messages, cursor_start=100, cursor_end=200, content_hash="second-hash",
    ), coalesce=False)


def test_a_newer_capture_of_a_session_waits_for_the_older_one(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    first = H["_capture"](ledger, session_hash="same")
    second = _second_capture(ledger, "same")
    other = H["_capture"](ledger, session_hash="other")
    assert first < second

    # Newest-first would take `second` (newer activity) before `first`: the guard must not.
    assert _picked(ledger, 6, 1) == [other, first] or _picked(ledger, 6, 1) == [first, other]
    assert second not in _picked(ledger, 6, 1)

    _set(ledger, first, state="consolidated")  # decided but not applied yet: still blocks
    assert second not in _picked(ledger, 6, 1)

    _set(ledger, first, state="applied")
    assert second in _picked(ledger, 6, 1)


def test_two_captures_of_one_session_are_consolidated_and_applied_in_order(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    first = H["_capture"](ledger, session_hash="same")
    second = _second_capture(ledger, "same")
    config = replace(DreamConfig(), idle_minutes=0, oldest_reserve=1, max_sessions=6)
    worker = DreamWorker(ledger, H["_service"](tmp_path), EXTRACTOR(), H["_Consolidator"](), config=config, now=lambda: NOW)

    worker.run(apply_candidates=True)
    assert (ledger.get_capture(first)["state"], ledger.get_capture(second)["state"]) == ("applied", "captured")

    worker.run(apply_candidates=True)
    assert ledger.get_capture(second)["state"] == "applied"
    assert ledger.get_capture(first)["updated_at"] <= ledger.get_capture(second)["updated_at"]
