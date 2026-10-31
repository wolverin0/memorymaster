"""Beat throughput: fresh scanned captures go first, the old backlog keeps a slot, 3 consolidations per tick."""

from __future__ import annotations

import runpy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.worker import DreamConfig, DreamWorker
from memorymaster.surfaces import scheduled_task

H = runpy.run_path("tests/test_dreaming_worker.py")
NOW = H["NOW"]
EXTRACTOR = runpy.run_path("tests/test_beat_worker_cap.py")["_PerCaptureExtractor"]


def _aged(ledger: DreamLedger, capture_id: int, age_days: int, *, fresh: bool) -> None:
    """Make a capture `age_days` old; `fresh` means a beat scan recorded its source mtime."""
    stamp = (NOW - timedelta(days=age_days) - timedelta(hours=1)).isoformat()
    with ledger._connect() as conn:
        conn.execute("UPDATE dream_captures SET last_activity_at=? WHERE id=?", (stamp, capture_id))
    if fresh:
        ledger.record_source_mtime(capture_id, (NOW - timedelta(days=age_days)).timestamp())


def _queue(ledger: DreamLedger, backlog: int, fresh: int) -> tuple[list[int], list[int]]:
    """`backlog` old captures (no source mtime), then `fresh` newer beat-scanned ones, by id."""
    old = [H["_capture"](ledger, session_hash=f"old{n}", extra_text=f" {n}") for n in range(backlog)]
    new = [H["_capture"](ledger, session_hash=f"new{n}", extra_text=f" {n}") for n in range(fresh)]
    for index, capture_id in enumerate(old):
        _aged(ledger, capture_id, 5, fresh=False)
    for index, capture_id in enumerate(new):
        _aged(ledger, capture_id, 0, fresh=True)
    return old, new


def _picked(ledger: DreamLedger, slots: int, reserve: int) -> list[int]:
    rows = ledger.eligible(idle_minutes=0, max_sessions=slots, now=NOW, oldest_reserve=reserve)
    return [int(row["id"]) for row in rows]


def test_a_fresh_scanned_capture_is_picked_before_an_older_backlog_capture(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    old, new = _queue(ledger, backlog=4, fresh=4)

    picked = _picked(ledger, slots=3, reserve=1)

    assert picked[:2] == new[:2]  # the two fresh slots go to the scanned captures, not to the older ids
    assert old[0] not in picked[:2]


def test_the_old_backlog_still_gets_a_slot_every_tick(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    old, new = _queue(ledger, backlog=4, fresh=4)

    picked = _picked(ledger, slots=3, reserve=1)

    assert picked == [new[0], new[1], old[0]]  # oldest backlog capture drains first


def test_unused_fresh_slots_fall_back_to_the_backlog(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    old, new = _queue(ledger, backlog=4, fresh=1)

    assert _picked(ledger, slots=3, reserve=1) == [new[0], old[0], old[1]]


def test_without_backlog_every_slot_is_fresh_and_nothing_repeats(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    _, new = _queue(ledger, backlog=0, fresh=5)

    assert _picked(ledger, slots=3, reserve=1) == new[:3]


def test_the_default_order_is_unchanged_for_the_dream_job(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    old, _ = _queue(ledger, backlog=4, fresh=4)

    rows = ledger.eligible(idle_minutes=0, max_sessions=3, now=NOW)

    assert [int(row["id"]) for row in rows] == old[:3]
    assert DreamConfig().oldest_reserve == 0 and DreamConfig.from_env().oldest_reserve == 0


def test_the_beat_tick_reserves_one_backlog_slot_and_consolidates_three_captures() -> None:
    assert scheduled_task.BEAT_MAX_SESSIONS == 3
    assert scheduled_task.BEAT_MAX_CONSOLIDATE_CAPTURES == 3
    assert scheduled_task.BEAT_OLDEST_RESERVE == 1


def test_one_beat_sized_run_consolidates_three_captures(tmp_path: Path) -> None:
    ledger = DreamLedger(tmp_path / "capture.db")
    old, new = _queue(ledger, backlog=3, fresh=3)
    consolidator = H["_Consolidator"]()
    config = replace(
        DreamConfig(), max_sessions=scheduled_task.BEAT_MAX_SESSIONS,
        max_consolidate_captures_per_run=scheduled_task.BEAT_MAX_CONSOLIDATE_CAPTURES,
        oldest_reserve=scheduled_task.BEAT_OLDEST_RESERVE, idle_minutes=0,
    )
    worker = DreamWorker(ledger, H["_service"](tmp_path), EXTRACTOR(), consolidator, config=config, now=lambda: NOW)

    summary = worker.run(apply_candidates=False)

    assert summary["consolidated"] == 3 and summary["deferred_consolidate_budget"] == 0 and summary["errors"] == 0
    states = {capture_id: ledger.get_capture(capture_id)["state"] for capture_id in old + new}
    assert [states[i] for i in (new[0], new[1], old[0])] == ["consolidated"] * 3  # fresh first, backlog got its slot
    assert [states[i] for i in (new[2], old[1], old[2])] == ["captured"] * 3  # the rest wait for the next tick
