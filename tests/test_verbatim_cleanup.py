"""Tests for the verbatim archive cleanup CLI (v3.23).

Synthetic populated verbatim DB exercises dedup, junk-prefix purge, dry-run
safety, FTS5 mirror sync, and the absent-table no-op.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.verbatim_cleanup import analyze, cleanup


_SCHEMA = """
CREATE TABLE verbatim_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'project',
    timestamp TEXT NOT NULL DEFAULT '',
    source_agent TEXT DEFAULT '',
    embedding_synced INTEGER NOT NULL DEFAULT 0
);
CREATE VIRTUAL TABLE verbatim_fts USING fts5(content);
"""


def _new_db(tmp_path: Path) -> Path:
    db = tmp_path / "v.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return db


def _seed(db: Path, rows: list[tuple[str, str, str]]) -> None:
    """rows: list of (session_id, role, content)."""
    conn = sqlite3.connect(str(db))
    for sess, role, content in rows:
        cur = conn.execute(
            "INSERT INTO verbatim_memories (session_id, role, content) VALUES (?, ?, ?)",
            (sess, role, content),
        )
        conn.execute("INSERT INTO verbatim_fts(rowid, content) VALUES (?, ?)", (cur.lastrowid, content))
    conn.commit()
    conn.close()


def _row_count(db: Path, table: str = "verbatim_memories") -> int:
    conn = sqlite3.connect(str(db))
    n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.close()
    return int(n)


def test_analyze_reports_composition(tmp_path):
    db = _new_db(tmp_path)
    _seed(db, [
        ("s1", "user", "hello world how are you today"),
        ("s1", "user", "hello world how are you today"),  # exact dup
        ("s2", "", "Rewrite ONLY the compiled truth section of this article"),  # junk + empty role
        ("s3", "assistant", "here is the answer to the question"),
    ])
    info = analyze(str(db))
    assert info["total"] == 4
    assert info["distinct_content"] == 3
    assert info["duplicate_extras"] == 1
    assert info["empty_role_rows"] == 1
    assert info["junk_prefix_rows"] == 1


def test_dedup_keeps_oldest_per_session_content(tmp_path):
    db = _new_db(tmp_path)
    _seed(db, [
        ("s1", "user", "alpha message body"),
        ("s1", "user", "alpha message body"),  # dup of #1
        ("s1", "user", "alpha message body"),  # dup of #1
        ("s2", "user", "alpha message body"),  # NOT a dup (different session)
    ])
    result = cleanup(str(db), dedup=True, purge_junk=False, dry_run=False)
    assert result["dedup_deleted"] == 2
    assert result["after_total"] == 2
    # The surviving id in s1 must be the smallest (oldest).
    conn = sqlite3.connect(str(db))
    survivors = sorted(int(r[0]) for r in conn.execute(
        "SELECT id FROM verbatim_memories WHERE session_id='s1'").fetchall())
    conn.close()
    assert survivors == [1]


def test_purge_junk_removes_known_prefixes(tmp_path):
    db = _new_db(tmp_path)
    _seed(db, [
        ("s1", "", "Rewrite ONLY the compiled truth section of this article"),
        ("s2", "", "You are a memory curator. Extract claims"),
        ("s3", "user", "this is a real user message"),
    ])
    result = cleanup(str(db), dedup=False, purge_junk=True, dry_run=False)
    assert result["junk_deleted"] == 2
    assert result["after_total"] == 1
    conn = sqlite3.connect(str(db))
    surviving = conn.execute("SELECT content FROM verbatim_memories").fetchone()[0]
    conn.close()
    assert "real user message" in surviving


def test_dry_run_deletes_nothing(tmp_path):
    db = _new_db(tmp_path)
    _seed(db, [
        ("s1", "user", "alpha body"),
        ("s1", "user", "alpha body"),
        ("s2", "", "Rewrite ONLY the compiled truth section"),
    ])
    before = _row_count(db)
    result = cleanup(str(db), dedup=True, purge_junk=True, dry_run=True)
    assert result["dry_run"] is True
    assert result["dedup_deleted"] == 1
    assert result["junk_deleted"] == 1
    assert _row_count(db) == before  # nothing actually deleted


def test_apply_syncs_fts_mirror(tmp_path):
    db = _new_db(tmp_path)
    _seed(db, [
        ("s1", "user", "alpha body content one"),
        ("s1", "user", "alpha body content one"),  # dup -> removed
    ])
    assert _row_count(db, "verbatim_fts") == 2
    cleanup(str(db), dedup=True, dry_run=False)
    # FTS rows should drop in lockstep with the source table.
    assert _row_count(db, "verbatim_memories") == 1
    assert _row_count(db, "verbatim_fts") == 1


def test_no_verbatim_table_returns_gracefully(tmp_path):
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()  # create empty DB
    info = analyze(str(db))
    assert info == {"verbatim_present": False}
    result = cleanup(str(db), dry_run=False)
    assert result == {"verbatim_present": False}


def test_rejects_postgres_dsn(tmp_path):
    with pytest.raises(ValueError, match="SQLite-only"):
        analyze("postgresql://localhost/x")
    with pytest.raises(ValueError, match="SQLite-only"):
        cleanup("postgresql://localhost/x")
