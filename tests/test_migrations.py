"""Tests for v3.20.0-S1 versioned migrations framework.

Coverage:
- discover_migrations finds the 0001 baseline
- MigrationRunner creates the schema_versions bookkeeping table
- apply_pending on a fresh DB stamps all known versions, idempotent re-run
  is a no-op
- Synthetic mid-version DB applies only the pending tail
- MigrationDriftError raised when an applied migration's source checksum
  no longer matches
- status() reports applied vs pending per migration
- CLI: `python -m memorymaster migrate --list`, `--status`, default-apply
  paths all work
"""
from __future__ import annotations

import datetime
import sqlite3
import sys
import textwrap
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from memorymaster import migrations
from memorymaster.migrations import (
    MigrationDriftError,
    MigrationRunner,
    discover_migrations,
)
from memorymaster.migrations.runner import Migration


@pytest.fixture
def sqlite_conn(tmp_path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def test_discover_finds_baseline():
    migs = discover_migrations()
    assert len(migs) >= 1
    versions = [m.version for m in migs]
    assert versions == sorted(versions)  # ascending order
    assert versions[0] == 1
    assert "baseline" in migs[0].description.lower()


def test_discover_each_migration_has_required_attrs():
    for m in discover_migrations():
        assert isinstance(m.version, int)
        assert isinstance(m.description, str) and m.description
        assert callable(m.apply_sqlite)
        assert callable(m.apply_postgres)
        # Checksum is sha256 hex (64 chars)
        assert len(m.checksum()) == 64
        int(m.checksum(), 16)  # valid hex


# ---------------------------------------------------------------------------
# MigrationRunner — bookkeeping
# ---------------------------------------------------------------------------


def test_runner_creates_schema_versions_table(sqlite_conn):
    MigrationRunner(sqlite_conn, backend="sqlite")
    rows = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_versions'"
    ).fetchall()
    assert len(rows) == 1


def test_runner_rejects_unknown_backend(sqlite_conn):
    with pytest.raises(ValueError, match="backend must be"):
        MigrationRunner(sqlite_conn, backend="mysql")


# ---------------------------------------------------------------------------
# apply_pending
# ---------------------------------------------------------------------------


def test_apply_pending_new_db_applies_all(sqlite_conn):
    runner = MigrationRunner(sqlite_conn, backend="sqlite")
    newly = runner.apply_pending()
    # At least the baseline gets applied
    assert 1 in newly
    # And it's recorded in the bookkeeping table
    rows = sqlite_conn.execute(
        "SELECT version, description FROM schema_versions ORDER BY version"
    ).fetchall()
    assert rows[0]["version"] == 1


def test_apply_pending_idempotent_rerun(sqlite_conn):
    runner = MigrationRunner(sqlite_conn, backend="sqlite")
    first = runner.apply_pending()
    second = runner.apply_pending()
    assert first  # something was applied first time
    assert second == []  # nothing on re-run


def test_apply_pending_mid_version_applies_tail(sqlite_conn, monkeypatch):
    """Simulate a DB at v0001 only, with a synthetic v0002 pending."""
    runner = MigrationRunner(sqlite_conn, backend="sqlite")
    runner.apply_pending()  # apply real baseline first

    # Build a synthetic v0002 migration via Migration dataclass directly,
    # then monkeypatch discover_migrations to return baseline + synthetic.
    sentinel = {"applied": False}

    def fake_apply_sqlite(conn):
        sentinel["applied"] = True
        conn.execute("CREATE TABLE migration_test_v2(id INTEGER)")

    def fake_apply_postgres(conn):  # noqa: ARG001
        sentinel["applied"] = True

    fake_path = Path(__file__)  # any real file — used only for checksum stability
    synthetic = Migration(
        version=2,
        description="synthetic test migration",
        module_name="tests.synthetic_v2",
        source_path=fake_path,
        apply_sqlite=fake_apply_sqlite,
        apply_postgres=fake_apply_postgres,
    )

    real_baseline = discover_migrations()[0]
    monkeypatch.setattr(
        "memorymaster.migrations.runner.discover_migrations",
        lambda: [real_baseline, synthetic],
    )

    newly = runner.apply_pending()
    assert newly == [2]  # only the tail
    assert sentinel["applied"] is True
    # The new table actually exists in the DB
    rows = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='migration_test_v2'"
    ).fetchall()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


def test_drift_detection_raises_when_applied_checksum_changes(sqlite_conn, monkeypatch):
    """Apply v1, then mutate the stored checksum (simulating a file edit
    after the fact) — re-running apply_pending must raise."""
    runner = MigrationRunner(sqlite_conn, backend="sqlite")
    runner.apply_pending()

    # Corrupt the stored checksum for v1 to simulate "the file was edited"
    sqlite_conn.execute(
        "UPDATE schema_versions SET checksum=? WHERE version=?",
        ("0" * 64, 1),
    )
    sqlite_conn.commit()

    runner2 = MigrationRunner(sqlite_conn, backend="sqlite")
    with pytest.raises(MigrationDriftError) as exc:
        runner2.apply_pending()
    assert "v0001" in str(exc.value)
    assert "immutable" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------


def test_status_reports_applied_and_pending(sqlite_conn, monkeypatch):
    """status() on a partly-applied DB shows applied=True for stamped versions
    and applied=False for not-yet-applied ones."""
    runner = MigrationRunner(sqlite_conn, backend="sqlite")
    runner.apply_pending()  # apply baseline

    # Inject a synthetic v0002 via the same monkeypatch trick
    real_baseline = discover_migrations()[0]
    synthetic = Migration(
        version=2,
        description="pending synthetic",
        module_name="tests.synthetic_v2",
        source_path=Path(__file__),
        apply_sqlite=lambda c: None,
        apply_postgres=lambda c: None,
    )
    monkeypatch.setattr(
        "memorymaster.migrations.runner.discover_migrations",
        lambda: [real_baseline, synthetic],
    )

    entries = runner.status()
    assert len(entries) == 2
    assert entries[0].version == 1
    assert entries[0].applied is True
    assert entries[0].applied_at is not None
    assert entries[1].version == 2
    assert entries[1].applied is False
    assert entries[1].applied_at is None


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_migrate_list_works(tmp_path, capsys):
    """`memorymaster migrate --list` enumerates known migrations without touching DB."""
    from memorymaster.cli import main

    db = tmp_path / "list.db"
    rc = main(["--db", str(db), "--workspace", str(tmp_path), "migrate", "--list"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "v0001" in captured.out
    assert "baseline" in captured.out.lower()
    # --list must not touch the DB
    assert not db.exists()


def test_cli_migrate_apply_works(tmp_path, capsys):
    """`memorymaster migrate` (no flags) applies pending migrations."""
    from memorymaster.cli import main
    from memorymaster.service import MemoryService

    db = tmp_path / "apply.db"
    # Need init_db first so the legacy schema is in place
    svc = MemoryService(db, workspace_root=tmp_path)
    svc.init_db()  # this already applies migrations through the runner

    capsys.readouterr()  # drain init noise
    rc = main(["--db", str(db), "--workspace", str(tmp_path), "migrate"])
    assert rc == 0
    captured = capsys.readouterr()
    # After init_db, baseline is already applied — second run is no-op
    assert "nothing to apply" in captured.out


def test_cli_migrate_status_works(tmp_path, capsys):
    """`memorymaster migrate --status` reports applied/pending."""
    from memorymaster.cli import main
    from memorymaster.service import MemoryService

    db = tmp_path / "status.db"
    svc = MemoryService(db, workspace_root=tmp_path)
    svc.init_db()
    capsys.readouterr()

    rc = main(["--db", str(db), "--workspace", str(tmp_path), "migrate", "--status"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "v0001" in captured.out
    assert "[applied]" in captured.out


def test_service_init_db_applies_migrations_automatically(tmp_path):
    """MemoryService.init_db() must trigger the runner so callers don't have
    to invoke `migrate` separately on first setup."""
    from memorymaster.service import MemoryService

    db = tmp_path / "auto.db"
    svc = MemoryService(db, workspace_root=tmp_path)
    svc.init_db()

    # schema_versions table exists and v0001 is recorded
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT version FROM schema_versions ORDER BY version"
        ).fetchall()
    finally:
        conn.close()
    assert (1,) in rows
