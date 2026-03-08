"""Crash-safe operator queue backed by SQLite WAL.

Replaces the JSON-file persistence for pending turns with an atomic
SQLite table.  Every enqueue / dequeue / ack / fail is a single
transaction -- no data loss on crash.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(slots=True, frozen=True)
class QueueEntry:
    """A single row in the pending_turns table."""

    id: int
    payload: str
    status: str
    inbox_offset: int
    created_at: str
    processed_at: str | None
    error: str | None


_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS pending_turns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    payload      TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'processing', 'done', 'failed')),
    inbox_offset INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL,
    processed_at TEXT,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_turns_status
    ON pending_turns (status, id);

CREATE TABLE IF NOT EXISTS queue_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class OperatorQueue:
    """Durable FIFO queue for operator turns, backed by SQLite WAL."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path),
            isolation_level="DEFERRED",
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._bootstrap()

    def _bootstrap(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.executescript(_SCHEMA_SQL)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Core queue operations
    # ------------------------------------------------------------------

    def enqueue(self, payload: str, inbox_offset: int = 0) -> int:
        """Insert a pending turn.  Returns the new row id."""
        cur = self._conn.execute(
            "INSERT INTO pending_turns (payload, status, inbox_offset, created_at) "
            "VALUES (?, 'pending', ?, ?)",
            (payload, inbox_offset, _utc_now_iso()),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def dequeue(self) -> QueueEntry | None:
        """Atomically claim the oldest pending entry.

        Sets status='processing' and returns the entry, or None if the
        queue is empty.  Uses a single UPDATE ... RETURNING to avoid
        TOCTOU races.
        """
        # SQLite < 3.35 doesn't support RETURNING, so we use a two-step
        # approach inside a single transaction for broad compatibility.
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute(
                "SELECT id FROM pending_turns "
                "WHERE status = 'pending' "
                "ORDER BY id LIMIT 1"
            )
            row = cur.fetchone()
            if row is None:
                self._conn.commit()
                return None
            row_id = row["id"]
            cur.execute(
                "UPDATE pending_turns SET status = 'processing' WHERE id = ?",
                (row_id,),
            )
            cur.execute("SELECT * FROM pending_turns WHERE id = ?", (row_id,))
            updated = cur.fetchone()
            self._conn.commit()
            return _row_to_entry(updated)
        except Exception:
            self._conn.rollback()
            raise

    def ack(self, entry_id: int) -> None:
        """Mark an entry as successfully processed."""
        self._conn.execute(
            "UPDATE pending_turns SET status = 'done', processed_at = ? "
            "WHERE id = ?",
            (_utc_now_iso(), entry_id),
        )
        self._conn.commit()

    def fail(self, entry_id: int, error: str) -> None:
        """Mark an entry as failed with an error message."""
        self._conn.execute(
            "UPDATE pending_turns SET status = 'failed', processed_at = ?, error = ? "
            "WHERE id = ?",
            (_utc_now_iso(), error, entry_id),
        )
        self._conn.commit()

    def requeue_processing(self) -> int:
        """On startup, reset any 'processing' entries back to 'pending'.

        This handles the case where the process crashed mid-processing.
        Returns the count of re-queued entries.
        """
        cur = self._conn.execute(
            "UPDATE pending_turns SET status = 'pending' "
            "WHERE status = 'processing'"
        )
        self._conn.commit()
        return cur.rowcount

    def pending_count(self) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM pending_turns "
            "WHERE status IN ('pending', 'processing')"
        )
        return cur.fetchone()["cnt"]

    def all_pending(self) -> list[QueueEntry]:
        """Return all pending/processing entries in FIFO order."""
        cur = self._conn.execute(
            "SELECT * FROM pending_turns "
            "WHERE status IN ('pending', 'processing') "
            "ORDER BY id"
        )
        return [_row_to_entry(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Metadata helpers (read_offset, acked_offset, counters)
    # ------------------------------------------------------------------

    def get_meta(self, key: str, default: str = "") -> str:
        cur = self._conn.execute(
            "SELECT value FROM queue_meta WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO queue_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_meta_int(self, key: str, default: int = 0) -> int:
        raw = self.get_meta(key, str(default))
        try:
            return int(raw)
        except (ValueError, TypeError):
            return default

    def set_meta_int(self, key: str, value: int) -> None:
        self.set_meta(key, str(value))

    # ------------------------------------------------------------------
    # Migration from JSON files
    # ------------------------------------------------------------------

    def migrate_from_json(
        self,
        queue_state_path: Path | None,
        state_path: Path | None,
        canonical_inbox: str,
    ) -> bool:
        """Import pending items from legacy JSON files.

        Returns True if migration happened, False if skipped (already
        migrated or no JSON files found).
        """
        if self.get_meta("migrated_from_json") == "true":
            return False

        migrated = False

        # Try queue_state_json first (newer format)
        if queue_state_path and queue_state_path.exists():
            try:
                raw = json.loads(queue_state_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    queue_inbox_raw = str(raw.get("inbox_jsonl", "")).strip()
                    queue_inbox = (
                        str(Path(queue_inbox_raw).resolve()) if queue_inbox_raw else ""
                    )
                    if queue_inbox == canonical_inbox:
                        self._import_queue_state(raw)
                        migrated = True
            except Exception:
                pass

        # Fall back to legacy state_json
        if not migrated and state_path and state_path.exists():
            try:
                raw = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    state_inbox_raw = str(raw.get("inbox_jsonl", "")).strip()
                    state_inbox = (
                        str(Path(state_inbox_raw).resolve()) if state_inbox_raw else ""
                    )
                    if state_inbox == canonical_inbox:
                        self._import_legacy_state(raw)
                        migrated = True
            except Exception:
                pass

        if migrated:
            self.set_meta("migrated_from_json", "true")

        return migrated

    def _import_queue_state(self, raw: dict[str, Any]) -> None:
        """Import from the queue_state_json format."""
        self.set_meta_int("read_offset", max(0, int(raw.get("read_offset", raw.get("offset", 0)))))
        self.set_meta_int(
            "acked_offset",
            max(0, int(raw.get("acked_offset", raw.get("offset", 0)))),
        )
        self.set_meta_int("seen_events", max(0, int(raw.get("seen_events", 0))))
        self.set_meta_int("processed_events", max(0, int(raw.get("processed_events", 0))))
        self.set_meta("inbox_jsonl", str(raw.get("inbox_jsonl", "")))

        pending = raw.get("pending", [])
        if isinstance(pending, list):
            for entry in pending:
                if not isinstance(entry, dict):
                    continue
                payload = str(entry.get("payload", "")).strip().lstrip("\ufeff")
                if not payload:
                    continue
                offset = max(0, int(entry.get("offset", 0)))
                self.enqueue(payload, inbox_offset=offset)

    def _import_legacy_state(self, raw: dict[str, Any]) -> None:
        """Import from the legacy state_json format."""
        offset = max(0, int(raw.get("offset", 0)))
        self.set_meta_int("read_offset", offset)
        self.set_meta_int("acked_offset", offset)
        self.set_meta_int("seen_events", max(0, int(raw.get("seen_events", 0))))
        self.set_meta_int("processed_events", max(0, int(raw.get("processed_events", 0))))
        self.set_meta("inbox_jsonl", str(raw.get("inbox_jsonl", "")))

    # ------------------------------------------------------------------
    # Cleanup / lifecycle
    # ------------------------------------------------------------------

    def purge_completed(self, keep_last: int = 100) -> int:
        """Delete old done/failed entries, keeping the most recent ones."""
        cur = self._conn.execute(
            "DELETE FROM pending_turns WHERE status IN ('done', 'failed') "
            "AND id NOT IN ("
            "  SELECT id FROM pending_turns "
            "  WHERE status IN ('done', 'failed') "
            "  ORDER BY id DESC LIMIT ?"
            ")",
            (keep_last,),
        )
        self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def __del__(self) -> None:
        self.close()


def _row_to_entry(row: sqlite3.Row) -> QueueEntry:
    return QueueEntry(
        id=row["id"],
        payload=row["payload"],
        status=row["status"],
        inbox_offset=row["inbox_offset"],
        created_at=row["created_at"],
        processed_at=row["processed_at"],
        error=row["error"],
    )
