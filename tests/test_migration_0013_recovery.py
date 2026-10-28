"""T-1865 / F15: migration 0013 on a legacy graph with orphaned rows.

0013 commits as it goes (``executescript``) and validates legacy rows last, so a refusal
leaves it half applied and not retryable. It cannot be edited (the runner checksums
applied migrations), so the limit is documented with a recovery procedure in
``docs/troubleshooting.md``. These tests pin the failure mode and prove the documented
SQL recovers the database; the SQL is read from the doc so the two cannot drift.
"""
from __future__ import annotations

import importlib
import re
import sqlite3
from pathlib import Path

import pytest

migration = importlib.import_module("memorymaster.stores.migrations.0013_canonical_entity_graph")
DOC = Path(__file__).resolve().parents[1] / "docs" / "troubleshooting.md"

_LEGACY = """
CREATE TABLE claims (id INTEGER PRIMARY KEY, text TEXT);
INSERT INTO claims VALUES (1, 'alpha');
CREATE TABLE entities (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
);
CREATE TABLE entity_edges (
    source_id TEXT NOT NULL, target_id TEXT NOT NULL, relation TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0, claim_id INTEGER, created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id, relation)
);
CREATE TABLE claim_entity_links (
    claim_id INTEGER NOT NULL, entity_id TEXT NOT NULL, PRIMARY KEY (claim_id, entity_id)
);
INSERT INTO entities VALUES ('e-alice', 'Alice', 'person', '[]', '2026-01-01T00:00:00Z');
INSERT INTO entities VALUES ('e-bob', 'Bob', 'person', '[]', '2026-01-01T00:00:00Z');
INSERT INTO entity_edges VALUES ('e-alice', 'e-bob', 'knows', 1.0, 1, '2026-01-01T00:00:00Z');
INSERT INTO entity_edges VALUES ('e-alice', 'e-ghost', 'knows', 1.0, 1, '2026-01-01T00:00:00Z');
"""


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(_LEGACY)
    yield c
    c.close()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _documented_recovery_sql() -> str:
    section = DOC.read_text(encoding="utf-8").split("## Migration 0013 stopped part-way", 1)[1]
    return re.search(r"```sql\n(.*?)```", section, re.S).group(1)


def test_orphaned_legacy_row_is_refused_and_leaves_a_half_applied_database(conn):
    with pytest.raises(RuntimeError, match="orphaned legacy graph rows"):
        migration.apply_sqlite(conn)
    assert {"entities_legacy_0013", "entity_edges_legacy_0013"} <= _tables(conn)


def test_retry_without_recovery_is_not_possible(conn):
    """Documented legacy-only limit: see docs/troubleshooting.md."""
    with pytest.raises(RuntimeError):
        migration.apply_sqlite(conn)
    with pytest.raises(sqlite3.OperationalError):
        migration.apply_sqlite(conn)


def test_documented_recovery_then_cleanup_lets_the_migration_succeed(conn):
    with pytest.raises(RuntimeError):
        migration.apply_sqlite(conn)

    conn.executescript(_documented_recovery_sql())
    assert not any(t.endswith("_legacy_0013") for t in _tables(conn))
    assert conn.execute("SELECT COUNT(*) FROM entity_edges").fetchone()[0] == 2  # nothing was lost

    conn.execute("DELETE FROM entity_edges WHERE target_id = 'e-ghost'")  # the operator's cleanup
    conn.commit()
    migration.apply_sqlite(conn)

    assert conn.execute("SELECT canonical_name FROM entities ORDER BY canonical_name").fetchall() == [
        ("Alice",), ("Bob",),
    ]
    assert conn.execute("SELECT COUNT(*) FROM entity_edges").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
