"""Tests for the SQLite WAL-backed operator queue."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from memorymaster.operator import MemoryOperator, OperatorConfig, TurnInput
from memorymaster.operator_queue import OperatorQueue
from memorymaster.service import MemoryService


def _tmp_db(prefix: str) -> Path:
    fd, raw = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".db", dir=".tmp_cases")
    os.close(fd)
    Path(raw).unlink(missing_ok=True)
    return Path(raw)


def _case_db(prefix: str) -> Path:
    return _tmp_db(f"sqlite-op-queue-{prefix}")


# ------------------------------------------------------------------
# Unit tests for OperatorQueue
# ------------------------------------------------------------------


class TestOperatorQueue:
    def test_enqueue_dequeue_ack(self) -> None:
        db = _tmp_db("q-basic")
        q = OperatorQueue(db)
        try:
            assert q.pending_count() == 0

            eid = q.enqueue('{"turn": 1}', inbox_offset=100)
            assert eid >= 1
            assert q.pending_count() == 1

            entry = q.dequeue()
            assert entry is not None
            assert entry.id == eid
            assert entry.payload == '{"turn": 1}'
            assert entry.status == "processing"
            assert entry.inbox_offset == 100

            q.ack(entry.id)
            assert q.pending_count() == 0

            # No more pending
            assert q.dequeue() is None
        finally:
            q.close()

    def test_enqueue_dequeue_fail(self) -> None:
        db = _tmp_db("q-fail")
        q = OperatorQueue(db)
        try:
            q.enqueue("bad-payload", inbox_offset=50)
            entry = q.dequeue()
            assert entry is not None

            q.fail(entry.id, "json_decode error")
            assert q.pending_count() == 0

            # Failed entries are not returned by dequeue
            assert q.dequeue() is None
        finally:
            q.close()

    def test_fifo_order(self) -> None:
        db = _tmp_db("q-fifo")
        q = OperatorQueue(db)
        try:
            q.enqueue("first", inbox_offset=10)
            q.enqueue("second", inbox_offset=20)
            q.enqueue("third", inbox_offset=30)

            e1 = q.dequeue()
            assert e1 is not None
            assert e1.payload == "first"
            q.ack(e1.id)

            e2 = q.dequeue()
            assert e2 is not None
            assert e2.payload == "second"
            q.ack(e2.id)

            e3 = q.dequeue()
            assert e3 is not None
            assert e3.payload == "third"
            q.ack(e3.id)

            assert q.dequeue() is None
        finally:
            q.close()

    def test_requeue_processing_on_crash_recovery(self) -> None:
        db = _tmp_db("q-requeue")
        q = OperatorQueue(db)
        try:
            q.enqueue("item-1", inbox_offset=10)
            q.enqueue("item-2", inbox_offset=20)

            # Simulate crash: dequeue but don't ack
            entry = q.dequeue()
            assert entry is not None
            assert entry.payload == "item-1"
            assert entry.status == "processing"
        finally:
            q.close()

        # Reopen -- simulates restart after crash
        q2 = OperatorQueue(db)
        try:
            requeued = q2.requeue_processing()
            assert requeued == 1

            # item-1 should be available again
            entry = q2.dequeue()
            assert entry is not None
            assert entry.payload == "item-1"
            q2.ack(entry.id)

            entry2 = q2.dequeue()
            assert entry2 is not None
            assert entry2.payload == "item-2"
            q2.ack(entry2.id)

            assert q2.pending_count() == 0
        finally:
            q2.close()

    def test_metadata_persistence(self) -> None:
        db = _tmp_db("q-meta")
        q = OperatorQueue(db)
        try:
            q.set_meta("inbox_jsonl", "/path/to/inbox.jsonl")
            q.set_meta_int("read_offset", 500)
            q.set_meta_int("seen_events", 10)
        finally:
            q.close()

        # Reopen and verify
        q2 = OperatorQueue(db)
        try:
            assert q2.get_meta("inbox_jsonl") == "/path/to/inbox.jsonl"
            assert q2.get_meta_int("read_offset") == 500
            assert q2.get_meta_int("seen_events") == 10
            assert q2.get_meta_int("nonexistent", 42) == 42
        finally:
            q2.close()

    def test_all_pending_returns_fifo_list(self) -> None:
        db = _tmp_db("q-all-pending")
        q = OperatorQueue(db)
        try:
            q.enqueue("a", inbox_offset=1)
            q.enqueue("b", inbox_offset=2)
            q.enqueue("c", inbox_offset=3)

            pending = q.all_pending()
            assert len(pending) == 3
            assert [e.payload for e in pending] == ["a", "b", "c"]
        finally:
            q.close()

    def test_purge_completed(self) -> None:
        db = _tmp_db("q-purge")
        q = OperatorQueue(db)
        try:
            for i in range(10):
                eid = q.enqueue(f"item-{i}", inbox_offset=i * 10)
                entry = q.dequeue()
                assert entry is not None
                q.ack(entry.id)

            deleted = q.purge_completed(keep_last=3)
            assert deleted == 7
        finally:
            q.close()

    def test_wal_mode_enabled(self) -> None:
        db = _tmp_db("q-wal")
        q = OperatorQueue(db)
        try:
            cur = q._conn.execute("PRAGMA journal_mode")
            mode = cur.fetchone()[0]
            assert mode == "wal"
        finally:
            q.close()


# ------------------------------------------------------------------
# Migration tests
# ------------------------------------------------------------------


class TestMigration:
    def test_migrate_from_queue_state_json(self) -> None:
        base = Path(".tmp_cases")
        base.mkdir(parents=True, exist_ok=True)

        inbox = base / "migrate_inbox.jsonl"
        inbox.write_text("", encoding="utf-8")
        canonical = str(inbox.resolve())

        queue_state_path = base / "migrate_queue_state.json"
        queue_state_path.write_text(
            json.dumps({
                "inbox_jsonl": canonical,
                "read_offset": 200,
                "acked_offset": 150,
                "seen_events": 5,
                "processed_events": 3,
                "next_queue_id": 6,
                "pending": [
                    {"entry_id": 4, "offset": 160, "payload": '{"turn": "pending-1"}'},
                    {"entry_id": 5, "offset": 180, "payload": '{"turn": "pending-2"}'},
                ],
            }),
            encoding="utf-8",
        )

        db = _tmp_db("q-migrate")
        q = OperatorQueue(db)
        try:
            result = q.migrate_from_json(queue_state_path, None, canonical)
            assert result is True

            assert q.get_meta_int("read_offset") == 200
            assert q.get_meta_int("acked_offset") == 150
            assert q.get_meta_int("seen_events") == 5
            assert q.get_meta_int("processed_events") == 3

            pending = q.all_pending()
            assert len(pending) == 2
            assert pending[0].payload == '{"turn": "pending-1"}'
            assert pending[1].payload == '{"turn": "pending-2"}'

            # Second migration should be skipped
            result2 = q.migrate_from_json(queue_state_path, None, canonical)
            assert result2 is False
        finally:
            q.close()

    def test_migrate_from_legacy_state_json(self) -> None:
        base = Path(".tmp_cases")
        base.mkdir(parents=True, exist_ok=True)

        inbox = base / "migrate_legacy_inbox.jsonl"
        inbox.write_text("", encoding="utf-8")
        canonical = str(inbox.resolve())

        state_path = base / "migrate_legacy_state.json"
        state_path.write_text(
            json.dumps({
                "inbox_jsonl": canonical,
                "offset": 100,
                "seen_events": 3,
                "processed_events": 3,
            }),
            encoding="utf-8",
        )

        db = _tmp_db("q-migrate-legacy")
        q = OperatorQueue(db)
        try:
            result = q.migrate_from_json(None, state_path, canonical)
            assert result is True

            assert q.get_meta_int("read_offset") == 100
            assert q.get_meta_int("acked_offset") == 100
            assert q.get_meta_int("seen_events") == 3
            assert q.pending_count() == 0
        finally:
            q.close()


# ------------------------------------------------------------------
# Integration tests: run_stream with SQLite queue
# ------------------------------------------------------------------


class TestRunStreamSqlite:
    def test_basic_stream_processing(self) -> None:
        db = _case_db("stream-basic")
        service = MemoryService(db, workspace_root=Path.cwd())
        service.init_db()

        queue_db = _tmp_db("q-stream-basic")
        operator = MemoryOperator(
            service,
            config=OperatorConfig(
                policy_mode="legacy",
                log_jsonl_path=None,
                state_json_path=None,
                queue_state_json_path=None,
                queue_journal_jsonl_path=None,
                queue_db_path=str(queue_db),
            ),
        )

        inbox = Path(".tmp_cases") / "q_stream_basic.jsonl"
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.write_text(
            '{"session_id":"s1","thread_id":"t1","turn_id":"turn-1","user_text":"Support email is test@example.com","assistant_text":"","observations":[]}\n'
            + "not-json\n",
            encoding="utf-8",
        )

        summary = operator.run_stream(inbox, poll_seconds=0.05, max_events=2)
        assert summary["seen_events"] == 2
        assert summary["processed_events"] == 1
        assert summary["json_errors"] == 1
        assert summary["exit_reason"] == "max_events_reached"

    def test_idle_timeout_with_sqlite_queue(self) -> None:
        db = _case_db("stream-idle")
        service = MemoryService(db, workspace_root=Path.cwd())
        service.init_db()

        queue_db = _tmp_db("q-stream-idle")
        operator = MemoryOperator(
            service,
            config=OperatorConfig(
                policy_mode="legacy",
                max_idle_seconds=0.25,
                log_jsonl_path=None,
                state_json_path=None,
                queue_state_json_path=None,
                queue_journal_jsonl_path=None,
                queue_db_path=str(queue_db),
            ),
        )

        inbox = Path(".tmp_cases") / "q_stream_idle.jsonl"
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.write_text("", encoding="utf-8")

        summary = operator.run_stream(inbox, poll_seconds=0.05)
        assert summary["processed_events"] == 0
        assert summary["seen_events"] == 0
        assert summary["exit_reason"] == "idle_timeout"

    def test_resume_from_sqlite_checkpoint(self) -> None:
        db = _case_db("stream-resume")
        service = MemoryService(db, workspace_root=Path.cwd())
        service.init_db()

        base = Path(".tmp_cases")
        queue_db = _tmp_db("q-stream-resume")
        inbox = base / "q_stream_resume.jsonl"
        inbox.write_text(
            '{"session_id":"s1","thread_id":"t1","turn_id":"turn-1","user_text":"Support email is first@example.com","assistant_text":"","observations":[]}\n'
            + '{"session_id":"s1","thread_id":"t1","turn_id":"turn-2","user_text":"Support email is second@example.com","assistant_text":"","observations":[]}\n',
            encoding="utf-8",
        )

        config = OperatorConfig(
            policy_mode="legacy",
            log_jsonl_path=None,
            state_json_path=None,
            queue_state_json_path=None,
            queue_journal_jsonl_path=None,
            queue_db_path=str(queue_db),
        )

        first_run = MemoryOperator(service, config=config).run_stream(
            inbox, poll_seconds=0.05, max_events=1
        )
        assert first_run["processed_events"] == 1
        assert first_run["seen_events"] == 1
        assert first_run["start_offset"] == 0
        assert first_run["turns"][0]["turn_id"] == "turn-1"

        second_run = MemoryOperator(service, config=config).run_stream(
            inbox, poll_seconds=0.05, max_events=1
        )
        assert second_run["processed_events"] == 1
        assert second_run["seen_events"] == 1
        assert second_run["start_offset"] == first_run["read_offset"]
        assert second_run["turns"][0]["turn_id"] == "turn-2"

    def test_crash_recovery_requeues_processing(self) -> None:
        """Simulate a crash mid-processing by leaving entries in 'processing' state."""
        db = _case_db("stream-crash")
        service = MemoryService(db, workspace_root=Path.cwd())
        service.init_db()

        queue_db = _tmp_db("q-stream-crash")
        inbox = Path(".tmp_cases") / "q_stream_crash.jsonl"
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.write_text(
            '{"session_id":"s1","thread_id":"t1","turn_id":"turn-1","user_text":"Support email is crash@example.com","assistant_text":"","observations":[]}\n',
            encoding="utf-8",
        )

        # Manually insert a "processing" entry to simulate crash
        q = OperatorQueue(queue_db)
        q.enqueue('{"session_id":"s1","thread_id":"t1","turn_id":"turn-0","user_text":"Crashed item","assistant_text":"","observations":[]}', inbox_offset=0)
        entry = q.dequeue()  # sets it to "processing"
        assert entry is not None
        assert entry.status == "processing"
        q.set_meta("inbox_jsonl", str(inbox.resolve()))
        q.set_meta_int("read_offset", 0)
        q.close()

        # Now run_stream should requeue the processing entry and process it
        config = OperatorConfig(
            policy_mode="legacy",
            max_idle_seconds=2.0,
            log_jsonl_path=None,
            state_json_path=None,
            queue_state_json_path=None,
            queue_journal_jsonl_path=None,
            queue_db_path=str(queue_db),
        )

        summary = MemoryOperator(service, config=config).run_stream(
            inbox, poll_seconds=0.05, max_events=10
        )
        # Should have processed the requeued item + the inbox item
        assert summary["processed_events"] == 2
        assert summary["seen_events"] == 1  # only 1 new from inbox
        assert summary["exit_reason"] == "idle_timeout"

    def test_sqlite_queue_with_utf8_bom(self) -> None:
        db = _case_db("stream-bom")
        service = MemoryService(db, workspace_root=Path.cwd())
        service.init_db()

        queue_db = _tmp_db("q-stream-bom")
        operator = MemoryOperator(
            service,
            config=OperatorConfig(
                policy_mode="legacy",
                log_jsonl_path=None,
                state_json_path=None,
                queue_state_json_path=None,
                queue_journal_jsonl_path=None,
                queue_db_path=str(queue_db),
            ),
        )

        inbox = Path(".tmp_cases") / "q_stream_bom.jsonl"
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.write_text(
            "\ufeff"
            + '{"session_id":"s1","thread_id":"t1","turn_id":"turn-bom","user_text":"Support email is bom@example.com","assistant_text":"","observations":[]}\n',
            encoding="utf-8",
        )

        summary = operator.run_stream(inbox, poll_seconds=0.05, max_events=1)
        assert summary["processed_events"] == 1
        assert summary["seen_events"] == 1
        assert summary["turns"][0]["turn_id"] == "turn-bom"

    def test_legacy_state_json_still_written(self) -> None:
        """When queue_db_path is set, state_json should still be written for backward compat."""
        db = _case_db("stream-compat")
        service = MemoryService(db, workspace_root=Path.cwd())
        service.init_db()

        base = Path(".tmp_cases")
        queue_db = _tmp_db("q-stream-compat")
        state_path = base / "q_stream_compat_state.json"
        state_path.unlink(missing_ok=True)

        operator = MemoryOperator(
            service,
            config=OperatorConfig(
                policy_mode="legacy",
                log_jsonl_path=None,
                state_json_path=str(state_path),
                queue_state_json_path=None,
                queue_journal_jsonl_path=None,
                queue_db_path=str(queue_db),
            ),
        )

        inbox = base / "q_stream_compat.jsonl"
        inbox.write_text(
            '{"session_id":"s1","thread_id":"t1","turn_id":"turn-1","user_text":"test","assistant_text":"","observations":[]}\n',
            encoding="utf-8",
        )

        summary = operator.run_stream(inbox, poll_seconds=0.05, max_events=1)
        assert summary["processed_events"] == 1

        # Legacy state.json should exist with correct data
        assert state_path.exists()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["processed_events"] == 1
        assert state["seen_events"] == 1
        assert state["offset"] > 0
