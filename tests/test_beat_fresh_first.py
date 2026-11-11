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
    FIRST_FAILURE_WAIT_MINUTES,
    SESSION_PREFIX,
    SLOW_ERROR_COUNT,
    UNCHARGED_RETRY_WAIT_MINUTES,
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


def test_large_and_charged_captures_never_take_a_beat_slot(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    small, large, charged = (H["_capture"](ledger, session_hash=f"s{n}") for n in range(3))
    _set(ledger, large, byte_count=SLOW_CAPTURE_BYTES)
    _set(ledger, charged, error_count=SLOW_ERROR_COUNT)

    assert _picked(ledger, 10, 2, lane=LANE_BEAT) == [small]
    assert sorted(_picked(ledger, 10, 2)) == sorted([small, large, charged])  # no lane rule: unchanged


def test_an_uncharged_retryable_capture_stays_in_the_beat_after_a_short_wait(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    fine, retry = (H["_capture"](ledger, session_hash=f"s{n}") for n in range(2))
    failed_at = datetime(2026, 7, 21, 13, tzinfo=timezone.utc)
    _set(ledger, retry, state="retryable", updated_at=failed_at.isoformat())

    def beat(at: datetime) -> list[int]:
        rows = ledger.eligible(idle_minutes=0, max_sessions=10, now=at, oldest_reserve=2, lane=LANE_BEAT)
        return sorted(int(row["id"]) for row in rows)

    assert beat(failed_at + timedelta(minutes=UNCHARGED_RETRY_WAIT_MINUTES - 1)) == [fine]
    assert beat(failed_at + timedelta(minutes=UNCHARGED_RETRY_WAIT_MINUTES + 1)) == sorted([fine, retry])


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


def test_the_dream_run_serves_the_ordinary_quota_before_due_slow_captures(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    old, new = _queue(ledger, backlog=0, fresh=5)
    slow = new[0]
    _set(ledger, slow, error_count=SLOW_ERROR_COUNT, updated_at=(NOW - timedelta(days=3)).isoformat())

    picked = ledger.eligible(idle_minutes=0, max_sessions=2, now=NOW, oldest_reserve=1, lane=LANE_DREAM, slow_slots=5)
    ids = [int(r["id"]) for r in picked]
    assert ids[-1] == slow and len(ids) == 3 and slow not in ids[:2]  # ordinary first, the slow one after them
    assert slow not in _picked(ledger, 2, 1, lane=LANE_BEAT)


def test_the_first_counted_failure_is_retried_by_the_beat_after_the_wait_the_second_goes_slow(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    capture = H["_capture"](ledger, session_hash="s")
    failed_at = datetime(2026, 7, 21, 13, tzinfo=timezone.utc)

    def lanes(at: datetime) -> tuple[bool, bool]:
        pick = lambda lane: capture in [  # noqa: E731
            int(r["id"]) for r in ledger.eligible(idle_minutes=0, max_sessions=5, now=at, oldest_reserve=2, lane=lane)]
        return pick(LANE_BEAT), pick(LANE_DREAM)

    _set(ledger, capture, state="retryable", error_count=1, updated_at=failed_at.isoformat())
    assert lanes(failed_at + timedelta(minutes=FIRST_FAILURE_WAIT_MINUTES - 1)) == (False, False)
    assert lanes(failed_at + timedelta(minutes=FIRST_FAILURE_WAIT_MINUTES + 1)) == (True, True)  # the beat retries it

    _set(ledger, capture, error_count=2)  # the second counted failure: slow lane, 10 h backoff
    assert lanes(failed_at + timedelta(hours=9)) == (False, False)
    assert lanes(failed_at + timedelta(hours=11)) == (False, True)  # only the 6-hourly run takes it


def test_the_dream_run_takes_due_slow_captures_beyond_its_ordinary_slots(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    slow = [H["_capture"](ledger, session_hash=f"slow{n}") for n in range(30)]
    fast = [H["_capture"](ledger, session_hash=f"fast{n}") for n in range(30)]
    for capture_id in slow:
        _set(ledger, capture_id, error_count=SLOW_ERROR_COUNT, updated_at=(NOW - timedelta(days=3)).isoformat())

    rows = ledger.eligible(idle_minutes=0, max_sessions=20, now=NOW, oldest_reserve=10, lane=LANE_DREAM, slow_slots=500)
    picked = [int(r["id"]) for r in rows]

    assert set(slow) <= set(picked) and len(picked) == 30 + 20  # every due slow one, plus the unchanged ordinary quota
    assert len([i for i in picked if i in fast]) == 20
    assert len(ledger.eligible(idle_minutes=0, max_sessions=20, now=NOW, oldest_reserve=10, lane=LANE_DREAM)) == 20


def test_the_backoff_doubles_with_every_failure_and_stops_at_the_cap(tmp_path: Path) -> None:
    from memorymaster.dreaming.ledger import SLOW_BACKOFF_MAX_HOURS, _backoff_hours

    hours = _backoff_hours()
    assert hours[0] == 0 and hours[1:4] == [5, 10, 20] and max(hours) == SLOW_BACKOFF_MAX_HOURS


# --- one session stays in order ----------------------------------------------------------------------------

def _next_capture(ledger: DreamLedger, session_hash: str, index: int) -> int:
    """Capture number ``index`` of one session: consecutive cursor range, texts ending in the index."""
    stamp = NOW - timedelta(hours=3) + timedelta(minutes=index)
    messages = (
        DreamMessage("m1", "user", "I prefer blue interfaces for daily work.", stamp.isoformat()),
        DreamMessage("m2", "assistant", f"Blue interfaces will be treated as your preference {index}", stamp.isoformat()),
    )
    return ledger.enqueue(CaptureEnvelope(
        provider="codex", session_hash=session_hash, scope="project:test", captured_at=NOW.isoformat(),
        last_activity_at=stamp.isoformat(), messages=messages, cursor_start=100 * index,
        cursor_end=100 * (index + 1), content_hash=f"hash-{index}",
    ), coalesce=False)


def test_a_session_contributes_an_in_order_prefix_and_picking_a_capture_picks_its_predecessors(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    chain = [_next_capture(ledger, "same", n) for n in range(6)]
    other = H["_capture"](ledger, session_hash="other")

    picked = _picked(ledger, 10, 2)

    assert [i for i in picked if i in chain] == chain[:SESSION_PREFIX]  # the next k, in id order, nothing later
    assert other in picked and picked.index(chain[0]) < picked.index(chain[1])


def test_the_prefix_counts_against_the_slots_and_never_skips_a_predecessor(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    chain = [_next_capture(ledger, "same", n) for n in range(3)]

    assert _picked(ledger, 2, 1) == chain[:2]  # the newest (chain[2]) needs 3 slots: not taken, the oldest is
    assert _picked(ledger, 1, 1) == chain[:1]


def test_a_gap_in_the_session_stops_the_prefix(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    chain = [_next_capture(ledger, "same", n) for n in range(3)]
    _set(ledger, chain[1], error_count=SLOW_ERROR_COUNT)  # charged twice: slow lane, not for the beat

    assert _picked(ledger, 10, 2, lane=LANE_BEAT) == chain[:1]  # chain[2] must not overtake chain[1]
    _set(ledger, chain[0], state="applied")
    assert _picked(ledger, 10, 2, lane=LANE_BEAT) == []
    _set(ledger, chain[1], state="applied")
    assert _picked(ledger, 10, 2, lane=LANE_BEAT) == chain[2:]


def test_an_older_consolidated_capture_blocks_the_newer_ones(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    first, second = (_next_capture(ledger, "same", n) for n in range(2))
    _set(ledger, first, state="consolidated")  # decided but not applied yet

    assert _picked(ledger, 10, 2) == []
    assert ledger.has_open_predecessor(second) and not ledger.has_open_predecessor(first)
    _set(ledger, first, state="applied")
    assert _picked(ledger, 10, 2) == [second] and not ledger.has_open_predecessor(second)


def test_three_captures_of_one_session_are_applied_in_order_in_one_run(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    chain = [_next_capture(ledger, "same", n) for n in range(3)]
    config = replace(DreamConfig(), idle_minutes=0, oldest_reserve=1, max_sessions=6)
    worker = DreamWorker(ledger, H["_service"](tmp_path), EXTRACTOR(), H["_Consolidator"](), config=config, now=lambda: NOW)

    summary = worker.run(apply_candidates=True)

    assert summary["applied"] == 3 and summary["errors"] == 0
    assert [ledger.get_capture(i)["state"] for i in chain] == ["applied"] * 3
    stamps = [ledger.get_capture(i)["updated_at"] for i in chain]
    assert stamps == sorted(stamps)


def test_a_failed_middle_capture_holds_back_the_third_at_extraction(tmp_path: Path) -> None:
    class FailsMiddle(EXTRACTOR):
        def extract(self, messages, *, scope, capture_hash):
            if str(messages[-1]["text"]).endswith("1"):
                raise ValueError("bad output")
            return super().extract(messages, scope=scope, capture_hash=capture_hash)

    ledger = DreamLedger(tmp_path / "capture.db")
    chain = [_next_capture(ledger, "same", n) for n in range(3)]
    config = replace(DreamConfig(), idle_minutes=0, oldest_reserve=1, max_sessions=6)
    worker = DreamWorker(ledger, H["_service"](tmp_path), FailsMiddle(), H["_Consolidator"](), config=config, now=lambda: NOW)

    worker.run(apply_candidates=True)

    assert [ledger.get_capture(i)["state"] for i in chain] == ["applied", "retryable", "captured"]


def test_a_middle_capture_that_fails_consolidation_keeps_the_third_from_being_applied(tmp_path: Path) -> None:
    class FailsMiddle(H["_Consolidator"]):
        def consolidate(self, candidates, current_claims, *, scope):
            if any(c.candidate_id.endswith("1") for c in candidates):
                raise ValueError("invalid decision")
            return super().consolidate(candidates, current_claims, scope=scope)

    ledger = DreamLedger(tmp_path / "capture.db")
    chain = [_next_capture(ledger, "same", n) for n in range(3)]
    config = replace(DreamConfig(), idle_minutes=0, oldest_reserve=1, max_sessions=6, max_consolidate_candidates=1)
    worker = DreamWorker(ledger, H["_service"](tmp_path), EXTRACTOR(), FailsMiddle(), config=config, now=lambda: NOW)

    worker.run(apply_candidates=True)

    states = [ledger.get_capture(i)["state"] for i in chain]
    assert states[0] == "applied" and states[1] == "retryable" and states[2] == "consolidated"  # not applied
