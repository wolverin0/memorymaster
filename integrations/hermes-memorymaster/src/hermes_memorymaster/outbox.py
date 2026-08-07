"""Bounded SQLite outbox for replay-safe Hermes-to-MemoryMaster delivery."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ACTIVE_STATES = ("pending", "leased", "retryable", "blocked")


class OutboxFullError(RuntimeError):
    """Raised before acceptance when durable outbox limits are exhausted."""


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    id: int
    replay_key: str
    envelope: dict[str, Any]
    status: str
    attempts: int


class DurableOutbox:
    def __init__(
        self,
        path: str | Path,
        *,
        max_pending: int,
        max_pending_bytes: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.max_pending = max_pending
        self.max_pending_bytes = max_pending_bytes
        self.clock = clock
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS outbox_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    replay_key TEXT NOT NULL UNIQUE,
                    envelope_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('pending','leased','retryable','blocked','completed','cancelled')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    lease_expires_at REAL,
                    last_error_code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_due
                    ON outbox_entries(status, next_attempt_at, id);
                """
            )
            now = self.clock()
            self._connection.execute(
                """UPDATE outbox_entries
                   SET status='retryable', lease_expires_at=NULL, updated_at=?
                   WHERE status='leased' AND lease_expires_at <= ?""",
                (now, now),
            )

    def enqueue(self, replay_key: str, envelope: dict[str, Any]) -> tuple[OutboxEntry, bool]:
        payload = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connection:
            existing = self._by_key(replay_key)
            if existing is not None:
                return existing, False
            count, used = self._active_usage()
            if count >= self.max_pending or used + len(payload.encode("utf-8")) > self.max_pending_bytes:
                raise OutboxFullError("MemoryMaster outbox bounded capacity is exhausted")
            now = self.clock()
            cursor = self._connection.execute(
                """INSERT INTO outbox_entries(
                       replay_key, envelope_json, status, attempts,
                       next_attempt_at, created_at, updated_at
                   ) VALUES (?, ?, 'pending', 0, ?, ?, ?)""",
                (replay_key, payload, now, now, now),
            )
            return self._by_id(int(cursor.lastrowid)), True

    def lease_next(self, *, lease_seconds: float = 30.0) -> OutboxEntry | None:
        with self._lock, self._connection:
            now = self.clock()
            self._connection.execute(
                """UPDATE outbox_entries
                   SET status='retryable', lease_expires_at=NULL,
                       last_error_code='lease_expired', updated_at=?
                   WHERE status='leased' AND lease_expires_at <= ?""",
                (now, now),
            )
            row = self._connection.execute(
                """SELECT id FROM outbox_entries
                   WHERE status IN ('pending','retryable') AND next_attempt_at <= ?
                   ORDER BY id LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            self._connection.execute(
                """UPDATE outbox_entries
                   SET status='leased', attempts=attempts+1,
                       lease_expires_at=?, updated_at=?
                   WHERE id=?""",
                (now + lease_seconds, now, int(row["id"])),
            )
            return self._by_id(int(row["id"]))

    def complete(self, entry_id: int) -> None:
        self._set_terminal(entry_id, "completed", None)

    def block(self, entry_id: int, error_code: str) -> None:
        self._set_terminal(entry_id, "blocked", error_code)

    def retry(self, entry_id: int, *, error_code: str, delay_seconds: float) -> None:
        with self._lock, self._connection:
            now = self.clock()
            self._connection.execute(
                """UPDATE outbox_entries
                   SET status='retryable', next_attempt_at=?, lease_expires_at=NULL,
                       last_error_code=?, updated_at=? WHERE id=?""",
                (now + delay_seconds, error_code, now, entry_id),
            )

    def _set_terminal(self, entry_id: int, status: str, error_code: str | None) -> None:
        with self._lock, self._connection:
            now = self.clock()
            completed_at = now if status == "completed" else None
            self._connection.execute(
                """UPDATE outbox_entries
                   SET status=?, lease_expires_at=NULL, last_error_code=?,
                       completed_at=?, updated_at=? WHERE id=?""",
                (status, error_code, completed_at, now, entry_id),
            )

    def counts(self) -> dict[str, int | str | None]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT status, COUNT(*) AS amount FROM outbox_entries GROUP BY status"
            ).fetchall()
            result: dict[str, int | str | None] = {
                state: 0
                for state in ("pending", "leased", "retryable", "blocked", "completed", "cancelled")
            }
            result.update({str(row["status"]): int(row["amount"]) for row in rows})
            last = self._connection.execute(
                """SELECT last_error_code FROM outbox_entries
                   WHERE last_error_code IS NOT NULL ORDER BY updated_at DESC LIMIT 1"""
            ).fetchone()
            result["last_error_code"] = str(last[0]) if last else None
            return result

    def make_due_for_test(self) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE outbox_entries SET next_attempt_at=0 WHERE status='retryable'"
            )

    def peek_for_test(self) -> OutboxEntry | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM outbox_entries ORDER BY id LIMIT 1"
            ).fetchone()
            return self._entry(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _active_usage(self) -> tuple[int, int]:
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        row = self._connection.execute(
            f"""SELECT COUNT(*), COALESCE(SUM(LENGTH(envelope_json)), 0)
                FROM outbox_entries WHERE status IN ({placeholders})""",
            ACTIVE_STATES,
        ).fetchone()
        return int(row[0]), int(row[1])

    def _by_key(self, replay_key: str) -> OutboxEntry | None:
        row = self._connection.execute(
            "SELECT * FROM outbox_entries WHERE replay_key=?", (replay_key,)
        ).fetchone()
        return self._entry(row) if row else None

    def _by_id(self, entry_id: int) -> OutboxEntry:
        row = self._connection.execute(
            "SELECT * FROM outbox_entries WHERE id=?", (entry_id,)
        ).fetchone()
        if row is None:
            raise KeyError(entry_id)
        return self._entry(row)

    @staticmethod
    def _entry(row: sqlite3.Row) -> OutboxEntry:
        return OutboxEntry(
            id=int(row["id"]),
            replay_key=str(row["replay_key"]),
            envelope=json.loads(str(row["envelope_json"])),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
        )
