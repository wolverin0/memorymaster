from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.models import CaptureEnvelope, DreamMessage


def _envelope(now: datetime, *, content_hash: str = "abc") -> CaptureEnvelope:
    return CaptureEnvelope(
        provider="codex",
        session_hash="session-hash",
        scope="project:test",
        captured_at=now.isoformat(),
        last_activity_at=(now - timedelta(hours=1)).isoformat(),
        messages=(DreamMessage("m1", "user", "Remember this stable preference.", now.isoformat()),),
        cursor_start=0,
        cursor_end=100,
        content_hash=content_hash,
    )


def test_enqueue_and_lease_are_idempotent_and_single_flight(tmp_path: Path) -> None:
    now = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
    ledger = DreamLedger(tmp_path / "dream.db")

    first = ledger.enqueue(_envelope(now))
    assert ledger.enqueue(_envelope(now)) == first
    assert ledger.acquire_lease("dream-worker", "worker-a", 60, now=now)
    assert not ledger.acquire_lease("dream-worker", "worker-b", 60, now=now)
    assert ledger.acquire_lease(
        "dream-worker", "worker-b", 60, now=now + timedelta(seconds=61)
    )


def test_enqueue_coalesces_contiguous_unprocessed_session_increments(tmp_path: Path) -> None:
    now = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
    ledger = DreamLedger(tmp_path / "dream.db")
    first = _envelope(now)
    second = CaptureEnvelope(
        provider=first.provider,
        session_hash=first.session_hash,
        scope=first.scope,
        captured_at=(now + timedelta(minutes=1)).isoformat(),
        last_activity_at=(now + timedelta(minutes=1)).isoformat(),
        messages=(
            DreamMessage(
                "m2", "assistant", "This preference remains durable.",
                (now + timedelta(minutes=1)).isoformat(),
            ),
        ),
        cursor_start=first.cursor_end,
        cursor_end=200,
        content_hash="def",
    )

    first_id = ledger.enqueue(first)
    second_id = ledger.enqueue(second)
    captured = ledger.get_capture(first_id)

    assert second_id == first_id
    assert captured["cursor_end"] == 200
    assert [message["message_id"] for message in captured["messages"]] == ["m1", "m2"]
    assert captured["turn_count"] == 2
    assert ledger.status()["queue"] == {"captured": 1}


def test_retention_never_discards_unprocessed_capture(tmp_path: Path) -> None:
    now = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
    ledger = DreamLedger(tmp_path / "dream.db")
    capture_id = ledger.enqueue(_envelope(now - timedelta(days=30)))

    result = ledger.prune(retain_days=7, max_bytes=1, now=now)

    assert result["deleted"] == 0
    assert ledger.get_capture(capture_id)["state"] == "captured"


def test_status_warns_on_stale_scheduler_and_low_structured_yield(tmp_path: Path) -> None:
    now = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
    ledger = DreamLedger(tmp_path / "dream.db")
    run_id = ledger.start_run(False, "gemini-3.5-flash", "glm-5.2", now=now - timedelta(hours=3))
    ledger.finish_run(run_id, "failed", {"reason": "provider"}, now=now - timedelta(hours=3))
    for index in range(10):
        ledger.record_provider_call(
            run_id,
            provider="zai",
            model="glm-5.2",
            outcome="ok" if index < 8 else "schema_error",
            latency_ms=100,
            structured_valid=index < 8,
            input_tokens=10,
            output_tokens=5,
            http_status=200,
            now=now - timedelta(minutes=index),
        )

    status = ledger.status(now=now, interval_minutes=60)

    assert "scheduler_stale" in status["warnings"]
    assert "zai_structured_yield_low" in status["warnings"]


def test_read_status_handles_pre_dream_capture_database_without_migrating(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE existing_capture_state (id INTEGER PRIMARY KEY)")

    status = DreamLedger.read_status(path)

    assert status["queue"] == {}
    assert status["warnings"] == ["dream_schema_missing"]
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"existing_capture_state"}
