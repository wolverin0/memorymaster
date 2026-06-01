"""Regression tests for the verbatim composite-index performance cluster.

Three findings, all anchored on WHY they matter (not on the exact SQL text):

1. [HIGH] The per-insert dedup probe
   ``WHERE session_id = ? AND content = ?`` must be served by a *seek* on a
   composite ``(session_id, content)`` index, not by re-reading every
   content blob in the session. Without the index SQLite can only use the
   single-column ``idx_verbatim_session`` and byte-compares the (up to
   ~262 KB) content of every other row in the session — O(rows-in-session)
   per insert, which let 9M+ rows accumulate on hot orchestrator sessions.

2. [HIGH] The cleanup dedup ``EXISTS`` self-join is effectively quadratic
   without the same index: its inner correlated probe degrades to a
   per-row content scan. With the composite index the inner probe is an
   index seek.

3. [MEDIUM] ``analyze`` must offer a fast path that does NOT run the two
   whole-table aggregations (``COUNT(DISTINCT content)`` / ``GROUP BY``),
   and must fold the junk-prefix counts into a single table pass — a
   read-only report should not re-read a multi-GB table several times.

The migration 0006 that ships the index is also asserted to be discoverable
and idempotent.
"""
from __future__ import annotations

import sqlite3

from memorymaster.verbatim_cleanup import analyze
from memorymaster.migrations.runner import MigrationRunner, discover_migrations


_SCHEMA_NO_COMPOSITE = """
CREATE TABLE verbatim_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    role TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'project',
    timestamp TEXT NOT NULL DEFAULT '',
    source_agent TEXT DEFAULT '',
    embedding_synced INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_verbatim_session ON verbatim_memories(session_id);
CREATE VIRTUAL TABLE verbatim_fts USING fts5(content);
"""

_DEDUP_SELECT = (
    "SELECT id FROM verbatim_memories WHERE session_id = 's1' AND content = 'x' LIMIT 1"
)
_CLEANUP_EXISTS = """
SELECT id FROM verbatim_memories AS v WHERE EXISTS (
    SELECT 1 FROM verbatim_memories AS older
    WHERE older.session_id IS v.session_id
      AND older.content = v.content
      AND older.id < v.id
)
"""


def _apply_index(conn: sqlite3.Connection) -> None:
    """Apply ONLY migration 0006's index via the runner (real migration path)."""
    MigrationRunner(conn, backend="sqlite").apply_pending()


def _plan(conn: sqlite3.Connection, sql: str) -> str:
    rows = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    return " | ".join(str(r[-1]) for r in rows)


def test_migration_0006_exists_and_creates_composite_index(tmp_path):
    """The composite index must ship as migration 0006 (it was genuinely
    absent — no 0005, schema.sql does not manage verbatim). WHY: the index is
    the load-bearing fix for both HIGH findings; if it is not in the migration
    set, production DBs never get it and dedup stays O(n) forever."""
    versions = {m.version for m in discover_migrations()}
    assert 6 in versions, "migration 0006 (verbatim composite index) must exist"

    db = tmp_path / "v.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA_NO_COMPOSITE)
    conn.commit()
    _apply_index(conn)
    idx = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_verbatim_session_content'"
    ).fetchone()
    conn.close()
    assert idx is not None, "migration 0006 must create idx_verbatim_session_content"


def test_migration_0006_is_idempotent_and_table_optional(tmp_path):
    """0006's apply step must be a no-op when verbatim_memories is absent
    (claims-only DB) and safe to re-run when present. WHY: the verbatim table
    is created out-of-band by the Stop hook, so the migration body may run on a
    DB that does not (yet) have it; an unguarded CREATE INDEX would crash every
    such migration run, and a non-idempotent CREATE would crash on re-run.

    We exercise the migration body directly (not the once-only runner) because
    table-optional + idempotent are properties of ``apply_sqlite`` itself."""
    mig = next(m for m in discover_migrations() if m.version == 6)

    # 1) No verbatim table at all -> apply must NOT raise and must NOT create it.
    bare = sqlite3.connect(":memory:")
    mig.apply_sqlite(bare)
    assert bare.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_verbatim_session_content'"
    ).fetchone() is None
    bare.close()

    # 2) Table present -> apply creates the index, and re-applying is a no-op.
    conn = sqlite3.connect(str(tmp_path / "present.db"))
    conn.executescript(_SCHEMA_NO_COMPOSITE)
    conn.commit()
    mig.apply_sqlite(conn)
    mig.apply_sqlite(conn)  # idempotent re-run must not raise
    idx = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_verbatim_session_content'"
    ).fetchone()
    conn.close()
    assert idx is not None


def test_dedup_probe_uses_index_seek_with_composite(tmp_path):
    """WHY: the per-insert dedup probe must be a SEARCH (seek) on the composite
    index, never a SCAN that re-reads every content blob in the session. We
    assert the plan flips from blob-scan to seek once 0006's index exists."""
    db = tmp_path / "v.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA_NO_COMPOSITE)
    conn.commit()

    before = _plan(conn, _DEDUP_SELECT)
    # Without the composite index the equality on `content` cannot be part of
    # the index seek — content is filtered row-by-row after a session seek.
    assert "idx_verbatim_session_content" not in before

    _apply_index(conn)
    after = _plan(conn, _DEDUP_SELECT)
    conn.close()
    assert "idx_verbatim_session_content" in after
    assert "SEARCH" in after  # seek, not SCAN


def test_cleanup_exists_join_is_index_seek_with_composite(tmp_path):
    """WHY: the cleanup EXISTS self-join is quadratic without the index — the
    inner correlated probe must seek on (session_id, content, id) rather than
    scan the session and byte-compare content per row."""
    db = tmp_path / "v.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA_NO_COMPOSITE)
    conn.commit()

    _apply_index(conn)
    plan = _plan(conn, _CLEANUP_EXISTS)
    conn.close()
    # The correlated inner probe must SEARCH the composite index using BOTH
    # session_id and content (a covering seek), not a bare session seek.
    assert "idx_verbatim_session_content" in plan
    assert "SEARCH older" in plan


def test_analyze_deep_false_skips_expensive_aggregations(tmp_path):
    """WHY: a read-only 'analyze' must not be forced to run COUNT(DISTINCT) and
    GROUP BY over a multi-GB table. deep=False returns the cheap counts and
    leaves the two expensive stats as None so the caller can choose."""
    db = tmp_path / "v.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA_NO_COMPOSITE)
    for sess, role, content in [
        ("s1", "user", "hello world how are you today"),
        ("s1", "user", "hello world how are you today"),
        ("s2", "", "Rewrite ONLY the compiled truth section of this article"),
    ]:
        conn.execute(
            "INSERT INTO verbatim_memories (session_id, role, content) VALUES (?,?,?)",
            (sess, role, content),
        )
    conn.commit()
    conn.close()

    fast = analyze(str(db), deep=False)
    assert fast["deep"] is False
    assert fast["total"] == 3
    assert fast["empty_role_rows"] == 1
    assert fast["junk_prefix_rows"] == 1
    # Expensive stats deliberately not computed.
    assert fast["distinct_content"] is None
    assert fast["duplicate_extras"] is None

    # Default (deep=True) still computes everything — legacy behaviour intact.
    full = analyze(str(db))
    assert full["deep"] is True
    assert full["distinct_content"] == 2
    assert full["duplicate_extras"] == 1
    assert full["junk_prefix_rows"] == 1


def test_analyze_junk_count_single_pass_matches_per_prefix(tmp_path):
    """WHY: folding the three junk-prefix LIKE scans into one pass must yield
    the IDENTICAL total the old per-prefix loop produced — the prefixes are
    disjoint, so one OR-ed scan equals three sequential ones."""
    from memorymaster import verbatim_cleanup as vc

    db = tmp_path / "v.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA_NO_COMPOSITE)
    contents = [
        "Rewrite ONLY the compiled truth section",
        "You are a memory curator and you do things",
        "You are an expert at compiling knowledge",
        "an ordinary user message that matches nothing",
    ]
    for c in contents:
        conn.execute(
            "INSERT INTO verbatim_memories (session_id, role, content) VALUES ('s', 'user', ?)",
            (c,),
        )
    conn.commit()

    # Reference: the old per-prefix sequential count.
    per_prefix = 0
    for prefix in vc._JUNK_PREFIXES:
        per_prefix += conn.execute(
            "SELECT COUNT(*) FROM verbatim_memories WHERE content LIKE ?",
            (prefix + "%",),
        ).fetchone()[0]
    single = vc._junk_prefix_count(conn)
    conn.close()

    assert single == per_prefix == 3
