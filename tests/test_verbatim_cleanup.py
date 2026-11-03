"""Tests for the verbatim archive cleanup CLI (v3.23).

Synthetic populated verbatim DB exercises dedup, junk-prefix purge, dry-run
safety, FTS5 mirror sync, and the absent-table no-op.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from datetime import datetime, timezone

from memorymaster.govern import verbatim_cleanup as vc
from memorymaster.govern.verbatim_cleanup import analyze, cleanup
from memorymaster.recall.verbatim_store import ensure_verbatim_schema


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


# --- T-2116: archive old rows into a separate DB, verified before delete ------------------------

_NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)
_OLD = "2026-04-08T18:47:56.080928+00:00"  # ~209 days before _NOW
_RECENT = "2026-10-20T10:00:00.000000+00:00"  # 14 days before _NOW
_COLS = ["id", "session_id", "role", "content", "scope", "timestamp", "source_agent",
         "embedding_synced", "created_at"]


def _real_db(tmp_path: Path, rows: list[tuple[int, str, str]]) -> Path:
    """Real verbatim schema (external-content FTS5); rows = (id, content, timestamp)."""
    db = tmp_path / "memorymaster.db"
    ensure_verbatim_schema(str(db))
    conn = sqlite3.connect(db)
    for rid, content, ts in rows:
        conn.execute(
            "INSERT INTO verbatim_memories (id, session_id, role, content, timestamp, created_at) "
            "VALUES (?, ?, 'user', ?, ?, ?)",
            (rid, f"s{rid % 3}", content, ts, ts),
        )
        conn.execute("INSERT INTO verbatim_fts(rowid, content) VALUES (?, ?)", (rid, content))
    conn.commit()
    conn.close()
    return db


def _count(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM verbatim_memories").fetchone()[0]
    finally:
        conn.close()


def _fts_hits(db: Path, term: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM verbatim_fts WHERE verbatim_fts MATCH ?", (term,)).fetchone()[0]
    finally:
        conn.close()


def _fts_integrity(db: Path) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO verbatim_fts(verbatim_fts, rank) VALUES('integrity-check', 1)")
    finally:
        conn.close()


def _old_and_recent(tmp_path: Path, n_old: int = 7) -> Path:
    rows = [(i, f"zebraquartz history {i}", _OLD) for i in range(1, n_old + 1)]
    rows += [(100, "recent kiwifruit turn", _RECENT), (101, "another kiwifruit turn", _RECENT)]
    return _real_db(tmp_path, rows)


def test_archive_dry_run_reports_and_touches_nothing(tmp_path: Path) -> None:
    db = _old_and_recent(tmp_path)
    before = db.read_bytes()

    out = vc.archive_old(str(db), days=90, now=_NOW)

    assert out["dry_run"] is True
    assert out["candidate_rows"] == 7
    assert db.read_bytes() == before
    assert not (tmp_path / vc.ARCHIVE_DB_NAME).exists()


def test_archive_moves_old_rows_with_their_fts_entries_and_verifies(tmp_path: Path) -> None:
    db = _old_and_recent(tmp_path)

    out = vc.archive_old(str(db), days=90, batch_size=3, dry_run=False, now=_NOW)

    archive = tmp_path / vc.ARCHIVE_DB_NAME
    assert out["rows_moved"] == 7 and out["batches"] == 3
    assert _count(db) == 2 and _count(archive) == 7
    # FTS followed the rows: gone from the source, searchable in the archive, both indexes sound.
    assert _fts_hits(db, "zebraquartz") == 0
    assert _fts_hits(archive, "zebraquartz") == 7
    assert _fts_hits(db, "kiwifruit") == 2
    _fts_integrity(db)
    _fts_integrity(archive)
    conn = sqlite3.connect(archive)
    ids = [r[0] for r in conn.execute("SELECT id FROM verbatim_memories ORDER BY id")]
    cols = [r[1] for r in conn.execute("PRAGMA table_info(verbatim_memories)")]
    conn.close()
    assert ids == list(range(1, 8))  # ids preserved
    assert cols == _COLS  # same schema


def test_archive_rerun_is_idempotent(tmp_path: Path) -> None:
    db = _old_and_recent(tmp_path)
    vc.archive_old(str(db), days=90, dry_run=False, now=_NOW)
    archive = tmp_path / vc.ARCHIVE_DB_NAME

    again = vc.archive_old(str(db), days=90, dry_run=False, now=_NOW)

    assert again["rows_moved"] == 0
    assert _count(db) == 2 and _count(archive) == 7
    _fts_integrity(archive)


def test_archive_crash_between_copy_and_delete_is_repaired_by_rerun(tmp_path: Path) -> None:
    db = _old_and_recent(tmp_path, n_old=4)
    vc.archive_old(str(db), days=90, dry_run=False, now=_NOW)
    archive = tmp_path / vc.ARCHIVE_DB_NAME
    # Simulate: the archive commit survived but the source delete did not (rows 1-4 back in source).
    src, dst = sqlite3.connect(db), sqlite3.connect(archive)
    for row in dst.execute(f"SELECT {', '.join(_COLS)} FROM verbatim_memories").fetchall():
        src.execute("INSERT INTO verbatim_memories VALUES (?,?,?,?,?,?,?,?,?)", row)
        src.execute("INSERT INTO verbatim_fts(rowid, content) VALUES (?, ?)", (row[0], row[3]))
    src.commit()
    src.close()
    dst.close()

    out = vc.archive_old(str(db), days=90, dry_run=False, now=_NOW)

    assert out["rows_moved"] == 4
    assert _count(db) == 2 and _count(archive) == 4
    _fts_integrity(db)
    _fts_integrity(archive)


def test_archive_hash_mismatch_rolls_back_and_keeps_the_source(tmp_path: Path) -> None:
    db = _old_and_recent(tmp_path)
    archive = tmp_path / vc.ARCHIVE_DB_NAME
    ensure_verbatim_schema(str(archive))
    conn = sqlite3.connect(archive)  # an unrelated row already owns id 3 in the archive
    conn.execute("INSERT INTO verbatim_memories (id, session_id, role, content, timestamp) "
                 "VALUES (3, 'x', 'user', 'DIFFERENT CONTENT', ?)", (_OLD,))
    conn.commit()
    conn.close()

    with pytest.raises(vc.ArchiveVerifyError):
        vc.archive_old(str(db), days=90, batch_size=100, dry_run=False, now=_NOW)

    assert _count(db) == 9  # nothing deleted from the source
    assert _count(archive) == 1  # the batch rolled back, no partial copy
    assert _fts_hits(db, "zebraquartz") == 7  # source FTS untouched
    _fts_integrity(db)


def test_archive_aborts_when_the_source_row_changes_before_delete(tmp_path: Path, monkeypatch) -> None:
    db = _old_and_recent(tmp_path, n_old=2)
    real_hash = vc._row_hash
    calls = {"n": 0}

    def drifting(row):  # calls 1-2 read the batch, 3-4 read it back from the archive, 5+ re-read the source
        calls["n"] += 1
        return real_hash(row) + ("x" if calls["n"] > 4 else "")

    monkeypatch.setattr(vc, "_row_hash", drifting)
    with pytest.raises(vc.ArchiveVerifyError):
        vc.archive_old(str(db), days=90, dry_run=False, now=_NOW)
    assert _count(db) == 4  # source rows survive


def test_archive_never_moves_unparseable_or_recent_timestamps(tmp_path: Path) -> None:
    db = _real_db(tmp_path, [(1, "odd one", "not-a-date"), (2, "empty ts", ""), (3, "old", _OLD),
                             (4, "recent", _RECENT)])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE verbatim_memories SET created_at='' WHERE id=2")
    conn.commit()
    conn.close()

    out = vc.archive_old(str(db), days=90, dry_run=False, now=_NOW)

    assert out["rows_moved"] == 1
    assert out["skipped_unparseable"] == 2
    assert _count(db) == 3


def test_archive_rejects_nonsense_arguments(tmp_path: Path) -> None:
    db = _old_and_recent(tmp_path)
    with pytest.raises(ValueError):
        vc.archive_old(str(db), days=0)
    with pytest.raises(ValueError):
        vc.archive_old(str(db), batch_size=0)
    with pytest.raises(ValueError):
        vc.archive_old(str(db), archive_path=str(db))
    with pytest.raises(ValueError, match="SQLite-only"):
        vc.archive_old("postgresql://x/y")


def test_retention_due_reads_only_the_oldest_row(tmp_path: Path) -> None:
    db = _old_and_recent(tmp_path)
    due = vc.retention_due(str(db), now=_NOW)
    assert due["due"] is True and due["oldest_age_days"] >= 200
    vc.archive_old(str(db), days=90, dry_run=False, now=_NOW)
    assert vc.retention_due(str(db), now=_NOW)["due"] is False
    assert vc.retention_due(str(tmp_path / "missing.db"), now=_NOW)["due"] is False


def test_verbatim_archive_cli_defaults_to_dry_run_and_apply_moves(tmp_path: Path, capsys) -> None:
    from memorymaster.surfaces.cli import main

    from datetime import timedelta

    wall = datetime.now(timezone.utc)  # the CLI uses the wall clock, so build the fixture relative to it
    old, recent = (wall - timedelta(days=200)).isoformat(), (wall - timedelta(days=1)).isoformat()
    db = _real_db(tmp_path, [(1, "old a", old), (2, "old b", old), (3, "recent a", recent)])

    assert main(["--db", str(db), "verbatim-archive", "--days", "90", "--dry-run"]) == 0
    assert "dry-run" in capsys.readouterr().out
    assert _count(db) == 3

    assert main(["--db", str(db), "verbatim-archive", "--days", "90", "--apply"]) == 0
    assert "applied" in capsys.readouterr().out
    assert _count(db) == 1 and _count(tmp_path / vc.ARCHIVE_DB_NAME) == 2


def test_archive_moves_a_row_that_was_never_fts_indexed(tmp_path: Path) -> None:
    """FTS drift on a real table must not abort the run (deleting an unindexed rowid raises 'malformed')."""
    db = _old_and_recent(tmp_path, n_old=2)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO verbatim_memories (id, session_id, role, content, timestamp, created_at) "
                 "VALUES (50, 's', 'user', 'unindexed history', ?, ?)", (_OLD, _OLD))
    conn.commit()
    conn.close()

    out = vc.archive_old(str(db), days=90, dry_run=False, now=_NOW)

    assert out["rows_moved"] == 3 and _count(db) == 2
    assert _fts_hits(db, "zebraquartz") == 0
    _fts_integrity(db)
