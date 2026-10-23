"""Contract tests for the SQLite project-source change cursor."""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest


cursor_migration = importlib.import_module("memorymaster.stores.migrations.0029_project_source_cursor")


_SOURCE_SCHEMA = """
CREATE TABLE claims (
    id INTEGER PRIMARY KEY,
    text TEXT,
    tenant_id TEXT,
    scope TEXT,
    updated_at TEXT,
    valid_from TEXT,
    valid_until TEXT
);
CREATE TABLE citations (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL,
    source TEXT,
    locator TEXT,
    excerpt TEXT,
    created_at TEXT
);
"""


def _open(path: Path | str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SOURCE_SCHEMA)
    cursor_migration.apply_sqlite(conn)
    return conn


def _clock(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT epoch, seq FROM project_source_clock WHERE singleton = 1").fetchone()


def _changes(conn: sqlite3.Connection) -> dict[int, int]:
    return {
        int(row["claim_id"]): int(row["seq"])
        for row in conn.execute("SELECT claim_id, seq FROM project_source_changes ORDER BY claim_id")
    }


def _claim(conn: sqlite3.Connection, claim_id: int, **overrides: object) -> None:
    fields: dict[str, object] = {
        "id": claim_id,
        "text": f"claim {claim_id}",
        "tenant_id": "tenant-a",
        "scope": "project:demo",
        "updated_at": "2026-10-22T00:00:00+00:00",
        "valid_from": "2026-10-01T00:00:00+00:00",
        "valid_until": None,
    }
    fields.update(overrides)
    conn.execute(
        f"INSERT INTO claims ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
        tuple(fields.values()),
    )


def test_schema_clock_and_indexes_are_metadata_only_and_idempotent() -> None:
    conn = _open()
    try:
        before = _clock(conn)
        assert len(before["epoch"]) == 32
        int(before["epoch"], 16)
        assert before["seq"] == 0
        assert _changes(conn) == {}
        assert cursor_migration.MERGE_COMPATIBLE is True

        cursor_migration.apply_sqlite(conn)
        assert dict(_clock(conn)) == dict(before)
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('index', 'trigger')")
        }
        assert {
            "idx_project_claims_updated",
            "idx_project_claims_valid_from",
            "idx_project_claims_valid_until",
            "idx_project_citations_created",
            "idx_project_citations_claim",
            "project_source_claims_ai",
            "project_source_claims_au",
            "project_source_claims_ad",
            "project_source_citations_ai",
            "project_source_citations_au",
            "project_source_citations_ad",
        } <= names
        assert [row["name"] for row in conn.execute("PRAGMA table_info(project_source_changes)")] == ["claim_id", "seq"]
    finally:
        conn.close()


def test_bare_database_is_a_noop_and_postgres_fails_closed() -> None:
    bare = sqlite3.connect(":memory:")
    try:
        cursor_migration.apply_sqlite(bare)
        assert bare.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'project_source_clock'"
        ).fetchone() is None
    finally:
        bare.close()

    with pytest.raises(RuntimeError, match="migration 29 is SQLite-only"):
        cursor_migration.apply_postgres(object())


@pytest.mark.parametrize("present_table", ["claims", "citations"])
def test_partial_source_schema_is_a_noop(present_table: str) -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(f"CREATE TABLE {present_table}(id INTEGER PRIMARY KEY)")
        cursor_migration.apply_sqlite(conn)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'project_source_clock'"
        ).fetchone() is None
    finally:
        conn.close()


def test_populated_pre29_store_upgrade_preserves_payload_without_lenient_schema_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from memorymaster.core.models import CitationInput
    from memorymaster.stores.storage import SQLiteStore

    database = tmp_path / "pre29.sqlite"
    store = SQLiteStore(database)
    original_apply = cursor_migration.apply_sqlite
    monkeypatch.setattr(cursor_migration, "apply_sqlite", lambda _conn: None)
    store.init_db()
    monkeypatch.setattr(cursor_migration, "apply_sqlite", original_apply)

    created = store.create_claim(
        "Legacy claim payload survives upgrade.",
        [CitationInput(source="fixture://pre29", locator="line:1", excerpt="Legacy evidence.")],
        scope="project:legacy",
        source_agent="fixture",
    )
    with sqlite3.connect(database) as conn:
        conn.execute("DELETE FROM schema_versions WHERE version = 29")

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="memorymaster.stores.storage"):
        store.init_db()

    assert not any("lenient schema initialization" in record.getMessage() for record in caplog.records)
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT text FROM claims WHERE id = ?", (created.id,)).fetchone() == (
            "Legacy claim payload survives upgrade.",
        )
        assert conn.execute(
            "SELECT source, locator, excerpt FROM citations WHERE claim_id = ?", (created.id,)
        ).fetchone() == ("fixture://pre29", "line:1", "Legacy evidence.")
        assert conn.execute("SELECT seq FROM project_source_clock WHERE singleton = 1").fetchone() == (0,)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = 'project_source_claims_ai'"
        ).fetchone() == (1,)


def test_real_sqlite_init_applies_cursor_after_creating_source_tables(tmp_path: Path) -> None:
    from memorymaster.stores.storage import SQLiteStore

    database = tmp_path / "fresh.sqlite"
    SQLiteStore(database).init_db()
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("SELECT version FROM schema_versions WHERE version = 29").fetchone() == (29,)
        assert conn.execute("SELECT seq FROM project_source_clock WHERE singleton = 1").fetchone() == (0,)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = 'project_source_claims_ai'"
        ).fetchone() == (1,)
    finally:
        conn.close()


def test_standalone_bare_runner_then_init_converges_cursor(tmp_path: Path) -> None:
    from memorymaster.stores.migrations import MigrationRunner
    from memorymaster.stores.storage import SQLiteStore

    database = tmp_path / "standalone-before-init.sqlite"
    bare = sqlite3.connect(database)
    try:
        assert 29 in MigrationRunner(bare, backend="sqlite").apply_pending()
    finally:
        bare.close()

    SQLiteStore(database).init_db()
    conn = sqlite3.connect(database)
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = 'project_source_claims_ai'"
        ).fetchone() == (1,)
    finally:
        conn.close()


def test_claim_changes_advance_sequence_retain_tombstone_and_rollback_atomically() -> None:
    conn = _open()
    try:
        _claim(conn, 1)
        assert _clock(conn)["seq"] == 1
        assert _changes(conn) == {1: 1}

        conn.execute("UPDATE claims SET scope = 'project:moved', updated_at = '2001-01-01' WHERE id = 1")
        assert _clock(conn)["seq"] == 2
        assert _changes(conn) == {1: 2}

        conn.execute("DELETE FROM claims WHERE id = 1")
        assert _clock(conn)["seq"] == 3
        assert _changes(conn) == {1: 3}

        conn.commit()
        committed = dict(_clock(conn))
        conn.execute("BEGIN")
        _claim(conn, 2)
        conn.execute("UPDATE claims SET text = 'transient' WHERE id = 2")
        assert _clock(conn)["seq"] == 5
        conn.rollback()
        assert dict(_clock(conn)) == committed
        assert _changes(conn) == {1: 3}
    finally:
        conn.close()


def test_claim_primary_key_move_marks_old_and_new_ids() -> None:
    conn = _open()
    try:
        _claim(conn, 1)
        conn.execute("UPDATE claims SET id = 2 WHERE id = 1")
        assert _clock(conn)["seq"] == 2
        assert _changes(conn) == {1: 2, 2: 2}
    finally:
        conn.close()


def test_citation_move_and_delete_mark_old_and_new_claims() -> None:
    conn = _open()
    try:
        _claim(conn, 1)
        _claim(conn, 2)
        conn.execute(
            "INSERT INTO citations(id, claim_id, source, created_at) VALUES (1, 1, 'session', '2026-10-22')"
        )
        assert _changes(conn) == {1: 3, 2: 2}

        conn.execute("UPDATE citations SET claim_id = 2, excerpt = 'moved evidence' WHERE id = 1")
        assert _clock(conn)["seq"] == 4
        assert _changes(conn) == {1: 4, 2: 4}

        conn.execute("DELETE FROM citations WHERE id = 1")
        assert _clock(conn)["seq"] == 5
        assert _changes(conn) == {1: 4, 2: 5}
    finally:
        conn.close()


def test_wal_late_commit_with_old_timestamps_uses_monotonic_sequence(tmp_path: Path) -> None:
    database = tmp_path / "cursor.sqlite"
    reader = _open(database)
    writer = sqlite3.connect(database)
    try:
        reader.execute("PRAGMA journal_mode=WAL")
        _claim(reader, 1)
        reader.commit()
        baseline = int(_clock(reader)["seq"])

        writer.execute("BEGIN")
        writer.execute("UPDATE claims SET updated_at = '2001-01-01T00:00:00+00:00' WHERE id = 1")
        writer.execute(
            "INSERT INTO claims(id, text, tenant_id, scope, updated_at) VALUES "
            "(2, 'late', 'tenant-a', 'project:demo', '2001-01-01T00:00:00+00:00')"
        )
        assert int(_clock(reader)["seq"]) == baseline
        writer.commit()

        assert int(_clock(reader)["seq"]) == baseline + 2
        assert _changes(reader) == {1: baseline + 1, 2: baseline + 2}
    finally:
        writer.close()
        reader.close()


def test_query_plans_use_project_source_indexes() -> None:
    conn = _open()
    try:
        for claim_id in range(1, 41):
            _claim(
                conn,
                claim_id,
                updated_at=f"2026-10-{claim_id:02d}",
                valid_from=f"2026-09-{claim_id:02d}",
                valid_until=f"2026-11-{claim_id:02d}",
            )
            conn.execute(
                "INSERT INTO citations(id, claim_id, created_at) VALUES (?, ?, ?)",
                (claim_id, claim_id, f"2026-10-{claim_id:02d}"),
            )
        plans = [
            (
                "SELECT id FROM claims WHERE tenant_id = ? AND scope = ? AND updated_at >= ? ORDER BY updated_at, id",
                ("tenant-a", "project:demo", "2026-10-01"),
                "idx_project_claims_updated",
            ),
            (
                "SELECT id FROM claims WHERE tenant_id = ? AND scope = ? AND valid_from <= ? ORDER BY valid_from, id",
                ("tenant-a", "project:demo", "2026-10-31"),
                "idx_project_claims_valid_from",
            ),
            (
                "SELECT id FROM claims WHERE tenant_id = ? AND scope = ? AND valid_until >= ? ORDER BY valid_until, id",
                ("tenant-a", "project:demo", "2026-10-01"),
                "idx_project_claims_valid_until",
            ),
            (
                "SELECT claim_id FROM citations WHERE created_at >= ? ORDER BY created_at, claim_id",
                ("2026-10-01",),
                "idx_project_citations_created",
            ),
            (
                "SELECT id FROM citations WHERE claim_id = ? ORDER BY id",
                (20,),
                "idx_project_citations_claim",
            ),
        ]
        for query, values, index in plans:
            detail = " ".join(row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {query}", values))
            assert index in detail
    finally:
        conn.close()


def test_backup_restore_preserves_cursor_and_trigger_behavior(tmp_path: Path) -> None:
    source_path = tmp_path / "source.sqlite"
    restored_path = tmp_path / "restored.sqlite"
    source = _open(source_path)
    try:
        _claim(source, 1)
        source.execute("INSERT INTO citations(id, claim_id, created_at) VALUES (1, 1, '2026-10-22')")
        source.commit()
        expected_clock = dict(_clock(source))
        expected_changes = _changes(source)
        restored = sqlite3.connect(restored_path)
        try:
            source.backup(restored)
        finally:
            restored.close()
    finally:
        source.close()

    restored = sqlite3.connect(restored_path)
    restored.row_factory = sqlite3.Row
    try:
        assert dict(_clock(restored)) == expected_clock
        assert _changes(restored) == expected_changes
        cursor_migration.apply_sqlite(restored)
        restored.execute("UPDATE claims SET text = 'restored edit' WHERE id = 1")
        assert int(_clock(restored)["seq"]) == int(expected_clock["seq"]) + 1
        assert _changes(restored) == {1: int(expected_clock["seq"]) + 1}
    finally:
        restored.close()
