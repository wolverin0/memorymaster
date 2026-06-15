"""Tests for the verbatim archive cleanup CLI (v3.23).

Synthetic populated verbatim DB exercises dedup, junk-prefix purge, dry-run
safety, FTS5 mirror sync, and the absent-table no-op.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.govern.verbatim_cleanup import analyze, cleanup


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


def _legacy_dedup_ids(db: Path) -> list[int]:
    """The pre-refactor `id NOT IN (SELECT MIN(id) ...)` anti-join.

    Kept verbatim here so the test pins the *result set* of the new
    NOT EXISTS query to the exact ids the old anti-join would delete — a
    behavioural anchor, not an implementation echo. If the optimized query
    ever diverges (e.g. mishandles NULL session_id or cross-session twins),
    this set comparison fails.
    """
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        """SELECT id FROM verbatim_memories WHERE id NOT IN (
               SELECT MIN(id) FROM verbatim_memories GROUP BY session_id, content
           )"""
    ).fetchall()
    conn.close()
    return sorted(int(r[0]) for r in rows)


def test_dedup_output_matches_legacy_anti_join(tmp_path):
    """The NOT EXISTS rewrite must delete EXACTLY the rows the old
    `id NOT IN (SELECT MIN(id)...)` anti-join would, across the tricky cases:
    multi-row dup groups, cross-session same-content (NOT dups), and a
    single-occurrence row (never a dup). WHY: the refactor is a pure
    performance change; any difference in the kept/dropped set silently
    corrupts the verbatim archive on the cold CLI cleanup path.
    """
    db = _new_db(tmp_path)
    _seed(db, [
        ("s1", "user", "shared body"),      # id 1 - oldest in (s1, shared) -> keep
        ("s1", "user", "shared body"),      # id 2 - dup -> drop
        ("s1", "user", "shared body"),      # id 3 - dup -> drop
        ("s2", "user", "shared body"),      # id 4 - different session -> keep
        ("s2", "user", "shared body"),      # id 5 - dup of id 4 -> drop
        ("s3", "assistant", "unique line"), # id 6 - single occurrence -> keep
    ])
    expected_drop = _legacy_dedup_ids(db)
    assert expected_drop == [2, 3, 5]  # sanity-pin the legacy contract itself

    survivors_before = {1, 4, 6}
    result = cleanup(str(db), dedup=True, purge_junk=False, dry_run=False)
    assert result["dedup_deleted"] == len(expected_drop)

    conn = sqlite3.connect(str(db))
    survivors = {int(r[0]) for r in conn.execute("SELECT id FROM verbatim_memories").fetchall()}
    conn.close()
    assert survivors == survivors_before


def test_dedup_handles_null_session_id(tmp_path):
    """NULL session_id rows with identical content are a real legacy-capture
    signature. `id NOT IN (SELECT MIN(id) GROUP BY session_id, content)` and
    the NOT EXISTS rewrite must agree: SQL GROUP BY buckets NULLs together,
    so the NOT EXISTS probe uses `IS` (not `=`) to match the same grouping.
    WHY: an `=` comparison would never match NULL=NULL and the new query would
    wrongly keep every NULL-session duplicate, diverging from the old result.
    """
    db = tmp_path / "nulls.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA.replace("session_id TEXT NOT NULL", "session_id TEXT"))
    # Insert NULL session_id duplicates directly.
    for content in ("dup body", "dup body", "dup body", "other body"):
        conn.execute(
            "INSERT INTO verbatim_memories (session_id, role, content) VALUES (NULL, '', ?)",
            (content,),
        )
    conn.commit()
    conn.close()

    expected_drop = _legacy_dedup_ids(db)
    assert expected_drop == [2, 3]  # ids 2,3 are NULL-session twins of id 1

    result = cleanup(str(db), dedup=True, purge_junk=False, dry_run=False)
    assert result["dedup_deleted"] == 2
    conn = sqlite3.connect(str(db))
    survivors = sorted(int(r[0]) for r in conn.execute("SELECT id FROM verbatim_memories").fetchall())
    conn.close()
    assert survivors == [1, 4]


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
