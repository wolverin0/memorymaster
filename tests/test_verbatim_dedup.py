"""Regression tests for verbatim_store.store_verbatim dedup.

The original dedup at verbatim_store.py:67-72 passed a sha256 hex prefix
to FTS5 MATCH against an index that contains the actual content text, not
the hash. Result: dedup never matched, every Stop event re-inserted every
message, and 9.07M omniclaude rows accumulated before discovery 2026-05-03
(see mm-0c43).

These tests guard the fix: a direct content compare on (session_id, content).
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from memorymaster.verbatim_store import store_transcript, store_verbatim


def _init_verbatim_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE verbatim_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'project',
            timestamp TEXT NOT NULL,
            source_agent TEXT DEFAULT '',
            embedding_synced INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        CREATE INDEX idx_verbatim_session ON verbatim_memories(session_id);
        CREATE INDEX idx_verbatim_scope ON verbatim_memories(scope);
        CREATE VIRTUAL TABLE verbatim_fts USING fts5(
            content,
            content='verbatim_memories',
            content_rowid='id'
        );
        CREATE TRIGGER verbatim_ai AFTER INSERT ON verbatim_memories BEGIN
            INSERT INTO verbatim_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER verbatim_ad AFTER DELETE ON verbatim_memories BEGIN
            INSERT INTO verbatim_fts(verbatim_fts, rowid, content)
            VALUES ('delete', old.id, old.content);
        END;
        """
    )
    conn.commit()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    db = tmp_path / "verbatim_dedup.db"
    conn = sqlite3.connect(str(db))
    _init_verbatim_schema(conn)
    conn.close()
    return str(db)


def _row_count(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM verbatim_memories").fetchone()[0]
    conn.close()
    return n


def test_first_insert_returns_row_id(db_path: str) -> None:
    rid = store_verbatim(db_path, "session-1", "user", "this is a real message worth storing")
    assert rid is not None
    assert _row_count(db_path) == 1


def test_second_insert_same_content_is_deduped(db_path: str) -> None:
    """The bug that caused 9.07M omniclaude rows: re-inserting same content
    on subsequent Stop events. Fixed dedup MUST return None on second call."""
    rid1 = store_verbatim(db_path, "session-1", "user", "message that should only store once")
    rid2 = store_verbatim(db_path, "session-1", "user", "message that should only store once")
    assert rid1 is not None
    assert rid2 is None, "second insert with identical (session_id, content) must dedup"
    assert _row_count(db_path) == 1


def test_same_content_different_session_inserts_both(db_path: str) -> None:
    """Dedup is scoped to session_id — same content in a different session
    is a legitimate separate row."""
    rid1 = store_verbatim(db_path, "session-1", "user", "shared content across sessions A")
    rid2 = store_verbatim(db_path, "session-2", "user", "shared content across sessions A")
    assert rid1 is not None
    assert rid2 is not None
    assert rid1 != rid2
    assert _row_count(db_path) == 2


def test_repeated_calls_simulate_orchestrator_burst(db_path: str) -> None:
    """The orchestrator pathology: same Stop event fires 100x on a session
    that grows by 1 message per fire. With working dedup, the table grows
    linearly (one new row per actual new message), not quadratically."""
    sid = "orchestrator-session"
    msgs = [f"orchestrator message number {i} with some content" for i in range(20)]

    # Simulate 5 Stop events, each "ingesting" a growing prefix of msgs
    for stop_event in range(1, 6):
        prefix = msgs[:stop_event * 4]  # 4, 8, 12, 16, 20 messages cumulative
        for m in prefix:
            store_verbatim(db_path, sid, "user", m)

    # Without dedup: 4+8+12+16+20 = 60 inserts
    # With working dedup: only 20 unique messages stored
    assert _row_count(db_path) == 20


def test_store_transcript_idempotent_on_re_call(db_path: str, tmp_path: Path) -> None:
    """End-to-end: store_transcript runs the same JSONL through twice
    (simulating two Stop events on a session). Second call should add zero
    new rows."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        '{"role": "user", "content": "first message of the conversation here"}\n'
        '{"role": "assistant", "content": "the assistant reply to the first message"}\n'
        '{"role": "user", "content": "second user message in this conversation flow"}\n',
        encoding="utf-8",
    )

    stats1 = store_transcript(db_path, str(transcript), scope="project:test")
    n1 = _row_count(db_path)

    stats2 = store_transcript(db_path, str(transcript), scope="project:test")
    n2 = _row_count(db_path)

    assert stats1["stored"] == 3
    assert n1 == 3
    assert stats2["stored"] == 0, "re-running the same transcript must add zero new rows"
    assert n2 == 3, "row count must stay stable across repeated stop events"


def test_short_content_is_filtered_not_deduped(db_path: str) -> None:
    """Content shorter than 20 chars is filtered before dedup (returns None
    for a different reason). Verify behavior unchanged."""
    rid = store_verbatim(db_path, "session-1", "user", "short")
    assert rid is None
    assert _row_count(db_path) == 0
