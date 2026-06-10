"""Pragma discipline on the verbatim store's connection path.

WHY: verbatim_memories is the hottest write path in the system (Stop hook +
MCP per-turn inserts), and the 2026-06-05 btree corruption was confined to
``idx_verbatim_session`` on this exact table. ``_connect`` historically set
WAL but NO busy_timeout, so the loser of a write race raised "database is
locked" immediately instead of waiting — dropped turns at best, and a
standing input to the corruption class at worst (P1 spec F5).
"""
from __future__ import annotations

from pathlib import Path

from memorymaster.recall.verbatim_store import _connect, store_verbatim


def test_connect_sets_busy_timeout(tmp_path: Path) -> None:
    """_connect must carry a non-zero busy_timeout.

    Intent: a concurrent writer on the hottest write path should WAIT for the
    lock rather than immediately raising ``database is locked`` and dropping
    the verbatim turn. Zero here re-opens the F5 root cause. 15000 ms is the
    P1 WAL-discipline uniform target.
    """
    conn = _connect(str(tmp_path / "verbatim.db"))
    try:
        timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()
    assert timeout_ms >= 15000, f"busy_timeout must be >= 15000ms, got {timeout_ms}"


def test_connect_sets_wal_mode(tmp_path: Path) -> None:
    """_connect must keep WAL journaling.

    Intent: WAL lets readers proceed while a writer holds the lock; regressing
    to rollback journal would serialize all ~12 fleet processes on this table.
    """
    conn = _connect(str(tmp_path / "verbatim.db"))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode == "wal", f"journal_mode must be wal, got {mode}"


def test_store_verbatim_still_writes_through_connect(tmp_path: Path) -> None:
    """The pragma change must not break the normal write path.

    Intent: guard against the busy_timeout pragma being added in a way that
    errors on a fresh DB (e.g. before the schema exists) — store_verbatim
    creates its own connection via _connect and must keep persisting rows.
    """
    db = str(tmp_path / "verbatim.db")
    # Minimal verbatim schema (same shape as tests/test_verbatim_store.py);
    # the real one is created by storage.py's _ensure_* passes.
    conn = _connect(db)
    try:
        conn.execute(
            """CREATE TABLE verbatim_memories (
                id INTEGER PRIMARY KEY,
                session_id TEXT,
                role TEXT,
                content TEXT,
                scope TEXT,
                timestamp TEXT,
                source_agent TEXT,
                embedding_synced INTEGER DEFAULT 0
            )"""
        )
        conn.execute("CREATE VIRTUAL TABLE verbatim_fts USING fts5(content)")
        conn.commit()
    finally:
        conn.close()

    row_id = store_verbatim(
        db,
        session_id="s1",
        role="user",
        content="a perfectly ordinary turn long enough to pass the length gate",
        source_agent="pytest",
    )
    assert row_id is not None
