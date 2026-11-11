"""Tests for v3.9.1 STRICT items.

S1: verbatim_recall import failure now logs WARNING once.
S2: claim_edges missing table now logs WARNING once via walk_neighbors.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import memorymaster.recall.claim_edges as claim_edges_module


def test_walker_logs_warning_when_table_missing(tmp_path, caplog):
    """S2 — when claim_edges table doesn't exist, walk_neighbors warns once."""
    # Reset the module-level flag so this test doesn't depend on order
    claim_edges_module._MISSING_TABLE_WARNED = False
    db = tmp_path / "no-edges.db"
    sqlite3.connect(str(db)).close()  # empty DB, no claim_edges table
    with caplog.at_level("WARNING", logger="memorymaster.recall.claim_edges"):
        result = claim_edges_module.walk_neighbors(db, [1, 2, 3], max_hops=2)
    assert result == {}
    assert any("claim_edges table missing" in r.message for r in caplog.records), \
        f"expected missing-table warning, got records: {[r.message for r in caplog.records]}"


def test_walker_warns_only_once_per_process(tmp_path, caplog):
    """S2 — second + third walks against missing table don't re-spam the log."""
    claim_edges_module._MISSING_TABLE_WARNED = False
    db = tmp_path / "no-edges.db"
    sqlite3.connect(str(db)).close()
    with caplog.at_level("WARNING", logger="memorymaster.recall.claim_edges"):
        claim_edges_module.walk_neighbors(db, [1], max_hops=1)
        caplog.clear()
        claim_edges_module.walk_neighbors(db, [2], max_hops=1)
        claim_edges_module.walk_neighbors(db, [3], max_hops=1)
    # Second + third walks should NOT produce new warning records
    assert not any("claim_edges table missing" in r.message for r in caplog.records), \
        "duplicate warning surfaced — _MISSING_TABLE_WARNED gate broken"


def test_walker_does_not_warn_when_table_exists(tmp_path, caplog):
    """When the table is present (even if empty), no warning fires."""
    claim_edges_module._MISSING_TABLE_WARNED = False
    db = tmp_path / "with-edges.db"
    conn = sqlite3.connect(str(db))
    try:
        claim_edges_module.ensure_claim_edges_schema(conn)
    finally:
        conn.close()
    with caplog.at_level("WARNING", logger="memorymaster.recall.claim_edges"):
        result = claim_edges_module.walk_neighbors(db, [1, 2], max_hops=2)
    assert result == {}  # empty table → no neighbors
    assert not any("claim_edges table missing" in r.message for r in caplog.records)


def test_module_exposes_warned_flag():
    """The flag is module-level so tests + production callers can reset it."""
    assert hasattr(claim_edges_module, "_MISSING_TABLE_WARNED")


def test_context_hook_exposes_verbatim_warned_flag():
    """S1 — _VERBATIM_IMPORT_WARNED gate exists so the warning is once-per-process."""
    from memorymaster.recall import context_hook
    assert hasattr(context_hook, "_VERBATIM_IMPORT_WARNED")


def test_context_hook_exposes_claim_edges_warned_flag():
    """S2 — _CLAIM_EDGES_MISSING_WARNED was reserved on context_hook for the F8 wiring."""
    from memorymaster.recall import context_hook
    assert hasattr(context_hook, "_CLAIM_EDGES_MISSING_WARNED")
