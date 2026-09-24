"""Dreaming ledger migrations trust the columns, not only the version rows.

Live 2026-09-23: the production capture-control.db had dream_schema_versions 1-3
recorded but no ``held_count`` column (a different build had written version 3),
so the first 4.9.0 Dreaming run failed with ``no such column: held_count`` and
extracted nothing. Opening the ledger must repair a missing column.
"""
from __future__ import annotations

import sqlite3

from memorymaster.dreaming.ledger import DreamLedger


def test_a_version_row_without_its_column_is_repaired_on_open(tmp_path):
    path = tmp_path / "capture-control.db"
    DreamLedger(path)  # current schema, versions 1-3 recorded
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE dream_captures DROP COLUMN held_count")
        conn.execute("ALTER TABLE dream_captures DROP COLUMN error_count")
        assert {row[0] for row in conn.execute("SELECT version FROM dream_schema_versions")} >= {1, 2, 3}

    DreamLedger(path)

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(dream_captures)")}
        assert {"held_count", "error_count", "resume_eligible", "extraction_run_id", "deferred_reason"} <= columns
        conn.execute("SELECT COUNT(*) FROM dream_captures WHERE held_count>0").fetchone()


def test_opening_a_current_ledger_twice_changes_nothing(tmp_path):
    path = tmp_path / "capture-control.db"
    DreamLedger(path)
    with sqlite3.connect(path) as conn:
        before = [tuple(row) for row in conn.execute("PRAGMA table_info(dream_captures)")]
    DreamLedger(path)
    with sqlite3.connect(path) as conn:
        assert [tuple(row) for row in conn.execute("PRAGMA table_info(dream_captures)")] == before


def test_a_column_present_without_its_version_row_does_not_crash_the_open(tmp_path):
    """Review of d09eb0c: the reverse drift (column added, version row missing) raised
    'duplicate column name' on open. Each numbered step skips a column that exists."""
    path = tmp_path / "capture-control.db"
    DreamLedger(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM dream_schema_versions WHERE version IN (2, 3)")

    DreamLedger(path)

    with sqlite3.connect(path) as conn:
        assert {row[0] for row in conn.execute("SELECT version FROM dream_schema_versions")} >= {1, 2, 3}
