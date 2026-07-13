"""Durable, finite controls for optional transcript capture paths."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_GLOBAL_DAILY_CALLS = 200
DEFAULT_PROVIDER_DAILY_CALLS = 100
DEFAULT_SESSION_DAILY_CALLS = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS capture_cursors (
    stream_key TEXT PRIMARY KEY,
    transcript_path_hash TEXT NOT NULL,
    prefix_hash TEXT NOT NULL,
    committed_offset INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capture_usage (
    reservation_id TEXT PRIMARY KEY,
    usage_day TEXT NOT NULL,
    provider TEXT NOT NULL,
    session_hash TEXT NOT NULL,
    operation TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'reserved',
    input_bytes INTEGER NOT NULL DEFAULT 0,
    output_bytes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_capture_usage_day ON capture_usage(usage_day);
CREATE TABLE IF NOT EXISTS capture_policy (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class CaptureLimits:
    global_daily_calls: int = DEFAULT_GLOBAL_DAILY_CALLS
    provider_daily_calls: int = DEFAULT_PROVIDER_DAILY_CALLS
    session_daily_calls: int = DEFAULT_SESSION_DAILY_CALLS

    @classmethod
    def from_env(cls) -> "CaptureLimits":
        return cls(
            global_daily_calls=_env_int("MEMORYMASTER_CAPTURE_GLOBAL_DAILY_CALLS", DEFAULT_GLOBAL_DAILY_CALLS),
            provider_daily_calls=_env_int("MEMORYMASTER_CAPTURE_PROVIDER_DAILY_CALLS", DEFAULT_PROVIDER_DAILY_CALLS),
            session_daily_calls=_env_int("MEMORYMASTER_CAPTURE_SESSION_DAILY_CALLS", DEFAULT_SESSION_DAILY_CALLS),
        )


@dataclass(frozen=True, slots=True)
class TranscriptChunk:
    stream_key: str
    transcript_path_hash: str
    prefix_hash: str
    start_offset: int
    end_offset: int
    text: str
    generation_reset: bool = False


@dataclass(frozen=True, slots=True)
class LLMReservation:
    reservation_id: str
    usage_day: str


def capture_state_path() -> Path:
    configured = os.environ.get("MEMORYMASTER_CAPTURE_STATE_DB", "").strip()
    return Path(configured) if configured else Path.home() / ".memorymaster" / "capture-control.db"


class CaptureLedger:
    def __init__(self, db_path: str | Path, *, limits: CaptureLimits | None = None) -> None:
        self.db_path = Path(db_path)
        self.limits = limits or CaptureLimits.from_env()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            now = datetime.now(timezone.utc).isoformat()
            conn.executemany(
                """INSERT INTO capture_policy VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                [
                    ("global_daily_calls", self.limits.global_daily_calls, now),
                    ("provider_daily_calls", self.limits.provider_daily_calls, now),
                    ("session_daily_calls", self.limits.session_daily_calls, now),
                ],
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()

    @staticmethod
    def _prefix(path: Path, limit: int = 4096) -> str:
        with path.open("rb") as handle:
            return hashlib.sha256(handle.read(max(0, limit))).hexdigest()

    def read_increment(self, transcript_path: str | Path, stream_key: str) -> TranscriptChunk:
        path = Path(transcript_path)
        path_hash = self._hash(str(path.resolve()))
        if not path.is_file():
            return TranscriptChunk(stream_key, path_hash, "", 0, 0, "")
        size = path.stat().st_size
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM capture_cursors WHERE stream_key = ?", (stream_key,)).fetchone()
        start = int(row["committed_offset"]) if row else 0
        comparison_hash = self._prefix(path, min(4096, start or size))
        generation_reset = bool(
            row
            and (
                row["transcript_path_hash"] != path_hash
                or row["prefix_hash"] != comparison_hash
                or size < start
            )
        )
        if generation_reset:
            start = 0
        with path.open("rb") as handle:
            handle.seek(start)
            pending = handle.read()
        last_newline = pending.rfind(b"\n")
        complete = pending[: last_newline + 1] if last_newline >= 0 else b""
        end = start + len(complete)
        committed_prefix = self._prefix(path, min(4096, end or size))
        return TranscriptChunk(
            stream_key,
            path_hash,
            committed_prefix,
            start,
            end,
            complete.decode("utf-8", errors="replace"),
            generation_reset,
        )

    def commit_cursor(self, chunk: TranscriptChunk) -> None:
        if chunk.end_offset <= chunk.start_offset:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            current = conn.execute(
                "SELECT committed_offset FROM capture_cursors WHERE stream_key=?",
                (chunk.stream_key,),
            ).fetchone()
            if current and not chunk.generation_reset and int(current[0]) > chunk.end_offset:
                return
            conn.execute(
                """INSERT INTO capture_cursors VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(stream_key) DO UPDATE SET
                     transcript_path_hash=excluded.transcript_path_hash,
                     prefix_hash=excluded.prefix_hash,
                     committed_offset=excluded.committed_offset,
                     updated_at=excluded.updated_at""",
                (chunk.stream_key, chunk.transcript_path_hash, chunk.prefix_hash, chunk.end_offset, now),
            )

    def reserve_llm(self, provider: str, session_id: str, operation: str) -> LLMReservation | None:
        usage_day = datetime.now(timezone.utc).date().isoformat()
        provider = (provider or "unknown").strip().lower()
        session_hash = self._hash(session_id or "unknown")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            global_count = conn.execute("SELECT COUNT(*) FROM capture_usage WHERE usage_day=?", (usage_day,)).fetchone()[0]
            provider_count = conn.execute("SELECT COUNT(*) FROM capture_usage WHERE usage_day=? AND provider=?", (usage_day, provider)).fetchone()[0]
            session_count = conn.execute("SELECT COUNT(*) FROM capture_usage WHERE usage_day=? AND session_hash=?", (usage_day, session_hash)).fetchone()[0]
            if global_count >= self.limits.global_daily_calls or provider_count >= self.limits.provider_daily_calls or session_count >= self.limits.session_daily_calls:
                conn.rollback()
                return None
            reservation_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO capture_usage (reservation_id, usage_day, provider, session_hash, operation, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (reservation_id, usage_day, provider, session_hash, operation, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            return LLMReservation(reservation_id, usage_day)
        finally:
            conn.close()

    def finish_llm(self, reservation: LLMReservation, *, input_bytes: int, output_bytes: int, outcome: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE capture_usage SET outcome=?, input_bytes=?, output_bytes=?, finished_at=? WHERE reservation_id=?",
                (outcome, max(0, input_bytes), max(0, output_bytes), datetime.now(timezone.utc).isoformat(), reservation.reservation_id),
            )

    def usage(self, usage_day: str | None = None) -> dict[str, object]:
        day = usage_day or datetime.now(timezone.utc).date().isoformat()
        with self._connect() as conn:
            totals = conn.execute("SELECT COUNT(*), COALESCE(SUM(input_bytes),0), COALESCE(SUM(output_bytes),0) FROM capture_usage WHERE usage_day=?", (day,)).fetchone()
            providers = conn.execute("SELECT provider, COUNT(*) FROM capture_usage WHERE usage_day=? GROUP BY provider ORDER BY provider", (day,)).fetchall()
        return {
            "usage_day": day,
            "global_calls": int(totals[0]),
            "input_bytes": int(totals[1]),
            "output_bytes": int(totals[2]),
            "providers": {str(row[0]): int(row[1]) for row in providers},
            "limits": {
                "global_daily_calls": self.limits.global_daily_calls,
                "provider_daily_calls": self.limits.provider_daily_calls,
                "session_daily_calls": self.limits.session_daily_calls,
            },
        }
