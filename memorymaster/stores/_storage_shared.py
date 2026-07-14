"""Shared constants and helper functions for the storage mixins.

Lives outside storage.py to avoid circular imports between the mixins and
the SQLiteStore class.

Also hosts the canonical SQLite connection helpers (``open_conn`` /
``connect_ro``) so every module in the package opens the shared DB with one
uniform pragma envelope instead of the historical ~55 divergent
``sqlite3.connect`` call sites (P1 WAL-discipline spec, F7).
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from memorymaster.core.retry import connect_with_retry

HUMAN_ID_PREFIX = "mm"
EVENT_HASH_ALGO = "sha256-v1"
TENANT_EVENT_HASH_ALGO = "sha256-tenant-v2"

SQLITE_EVENTS_APPEND_ONLY_TRIGGERS = (
    "trg_events_append_only_update",
    "trg_events_append_only_delete",
)
SQLITE_CONFIRMED_TUPLE_GUARD_TRIGGERS = (
    "trg_claims_confirmed_tuple_guard_insert",
    "trg_claims_confirmed_tuple_guard_update",
)


def compute_tenant_event_hash(
    *,
    tenant_id: str,
    event_hash: str,
    tenant_prev_event_hash: str | None,
) -> str:
    """Commit one global event hash into a tenant-partitioned hash chain."""
    material = "\x1f".join(
        (
            TENANT_EVENT_HASH_ALGO,
            tenant_id,
            event_hash,
            tenant_prev_event_hash or "",
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def generate_human_id_hash(text: str) -> str:
    """Generate a 4-hex-char hash from text for human-readable IDs."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return digest[:4]


def generate_top_level_human_id(subject: str | None, text: str) -> str:
    """Generate a top-level human_id like ``mm-a3f8``."""
    seed = (subject or text).strip()
    return f"{HUMAN_ID_PREFIX}-{generate_human_id_hash(seed)}"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


DEFAULT_BUSY_TIMEOUT_MS = 15000
DEFAULT_RO_BUSY_TIMEOUT_MS = 2000

# Process-local busy-error counter (P1 spec §2.10): every "database is
# locked/busy" failure inside open_conn's retry wrapper increments it. The
# steward persists the value per cycle (jobs/integrity.emit_metrics) — a
# rising trend despite uniform 15 s timeouts is escalation tripwire (d) in
# spec §7, so it must be counted, not just logged.
_busy_errors = 0


def record_busy_error() -> None:
    """Count one busy/locked connection failure (process-local)."""
    global _busy_errors
    _busy_errors += 1


def busy_error_count() -> int:
    """Busy/locked failures seen by this process since import."""
    return _busy_errors


def _is_busy_error(exc: sqlite3.Error) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def open_conn(
    db_path: str | Path,
    *,
    busy_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Open a read-write SQLite connection with the uniform pragma envelope.

    Single place that sets the operating envelope for every writer in the
    fleet: ``row_factory=Row``, ``foreign_keys=ON``, ``journal_mode=WAL``
    and ``busy_timeout`` (default 15000 ms — up from the divergent
    0/5000/30000 ms found across ad-hoc sites). Without busy_timeout, the
    loser of a write race raises an unhandled "database is locked"
    OperationalError that aborts the ingest/transition and LOSES the write.
    Make the loser wait instead.

    Wrapped in ``connect_with_retry`` so transient open failures back off
    exponentially before giving up (retry.py).

    ``check_same_thread=False`` is for callers that share one connection
    across threads behind their own locking (operator_queue) — default True
    matches sqlite3.connect.
    """

    def _open() -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(str(db_path), check_same_thread=check_same_thread)
        except sqlite3.OperationalError as exc:
            if _is_busy_error(exc):
                record_busy_error()
            raise
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(f"PRAGMA busy_timeout = {int(busy_ms)}")
        except sqlite3.OperationalError as exc:
            conn.close()
            if _is_busy_error(exc):
                record_busy_error()
            raise
        return conn

    return connect_with_retry(_open)


def connect_ro(db_path: str | Path, *, query_ms: int = DEFAULT_RO_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open a strictly read-only SQLite connection.

    Uses the ``file:...?mode=ro`` URI plus ``query_only=ON`` so the
    connection CANNOT take a write lock — any write attempt raises
    ``sqlite3.OperationalError`` instead of silently contending with the
    fleet's writers. Pattern already proven in-tree (recall_tokenizer,
    verbatim_recall, session-start hook). ``query_ms`` is the busy_timeout
    for readers waiting on a checkpoint.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(query_ms)}")
    return conn


class ConcurrentModificationError(RuntimeError):
    """Raised when an optimistic-lock check fails during a status transition."""
