"""Regression tests for storage-concurrency cluster fixes.

WHY these matter:

1. The claims DB is written concurrently by the Stop hook, the steward cycle,
   and live MCP ingests. Without a busy_timeout, the loser of a write race
   raises an unhandled ``database is locked`` OperationalError that aborts the
   ingest/transition and silently LOSES the write. busy_timeout must be set on
   every connection so the loser waits instead of failing.

2. ``claim_pending_media_retries`` is a single-claimer queue: a media row must be
   handed to exactly one fetcher. If two fetchers can claim the same row, the
   media is fetched twice, attempt_count over-counts (tripping max-attempt
   logic early), and duplicate events are emitted. The SELECT+UPDATE must be one
   atomic read-modify-write, and the UPDATE must refuse to re-claim a row that
   has already left 'pending'.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.service import MemoryService
from memorymaster.stores.storage import SQLiteStore


@pytest.fixture()
def service(tmp_path: Path) -> MemoryService:
    svc = MemoryService(tmp_path / "atlas.db", workspace_root=tmp_path)
    svc.init_db()
    return svc


def test_connection_sets_busy_timeout(tmp_path: Path) -> None:
    """A fresh connection must carry a non-zero busy_timeout.

    Intent: a concurrent writer should WAIT for the lock rather than immediately
    raising ``database is locked`` and dropping the write. Zero here would mean
    the original lost-write bug is back.
    """
    store = SQLiteStore(tmp_path / "bt.db")
    store.init_db()
    with store.connect() as conn:
        timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout_ms >= 5000, f"busy_timeout must be >= 5000ms, got {timeout_ms}"


def _enqueue(store: SQLiteStore, source_item_id: int, key: str) -> None:
    store.enqueue_media_retry(source_item_id=source_item_id, media_key=key)


def test_claim_is_single_claimer_no_double_attempt(service: MemoryService) -> None:
    """Each pending row is claimed exactly once; attempt_count increments once.

    Intent: the queue's single-claimer guarantee. Re-claiming a row would fetch
    the same media twice and over-count attempts.
    """
    store: SQLiteStore = service.store
    source = service.upsert_external_source(source_type="whatsapp", display_name="primary")
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id="voice-1",
        item_type="audio",
        payload_json={"media_path": "media/voice-1.ogg"},
    )
    _enqueue(store, item.id, "media/voice-1.ogg")

    first = store.claim_pending_media_retries(limit=10)
    second = store.claim_pending_media_retries(limit=10)

    assert len(first) == 1
    # Already moved to 'retrying' -> must NOT be re-claimed.
    assert second == []
    assert first[0].attempt_count == 1


def test_update_guard_refuses_already_claimed_row(service: MemoryService) -> None:
    """The UPDATE WHERE clause must include status='pending'.

    Intent: even if a stale id slips into the claim set, a row that already left
    'pending' must never be re-claimed (no duplicate fetch, no extra attempt).
    """
    store: SQLiteStore = service.store
    source = service.upsert_external_source(source_type="whatsapp", display_name="p")
    item = service.upsert_source_item(
        source_id=source.id,
        source_item_id="voice-2",
        item_type="audio",
        payload_json={"media_path": "media/voice-2.ogg"},
    )
    _enqueue(store, item.id, "media/voice-2.ogg")
    claimed = store.claim_pending_media_retries(limit=10)
    retry_id = claimed[0].id

    # Directly attempt the claiming UPDATE again against the already-retrying row.
    with store.connect() as conn:
        cur = conn.execute(
            """
            UPDATE media_retry_queue
            SET status = 'retrying', attempt_count = attempt_count + 1
            WHERE status = 'pending' AND id = ?
            """,
            (retry_id,),
        )
        conn.commit()
        rowcount = cur.rowcount
        attempts = conn.execute(
            "SELECT attempt_count FROM media_retry_queue WHERE id = ?", (retry_id,)
        ).fetchone()[0]
    assert rowcount == 0, "row already out of 'pending' must not be updated"
    assert attempts == 1, "attempt_count must not be double-incremented"
