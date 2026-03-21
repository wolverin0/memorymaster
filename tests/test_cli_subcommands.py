"""Tests for CLI subcommands not covered elsewhere."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memorymaster.cli import main


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    db = tmp_path / "test.db"
    main(["--db", str(db), "init-db"])
    return db


def _run(capsys, argv):
    capsys.readouterr()
    rc = main(argv)
    out = capsys.readouterr().out.strip()
    return rc, out


def _ingest(db, text="Test claim", source="test.py"):
    return main([
        "--db", str(db), "ingest",
        "--text", text,
        "--source", source,
    ])


class TestInitDb:
    def test_creates_db(self, tmp_path, capsys):
        db = tmp_path / "new.db"
        rc, out = _run(capsys, ["--db", str(db), "init-db"])
        assert rc == 0
        assert db.exists()

    def test_json_output(self, tmp_path, capsys):
        db = tmp_path / "new2.db"
        rc, out = _run(capsys, ["--db", str(db), "--json", "init-db"])
        assert rc == 0
        data = json.loads(out)
        assert data["ok"] is True


class TestIngest:
    def test_basic_ingest(self, tmp_db, capsys):
        rc, out = _run(capsys, [
            "--db", str(tmp_db), "ingest",
            "--text", "Python is great",
            "--source", "test.py",
        ])
        assert rc == 0
        assert "claim_id=" in out

    def test_json_ingest(self, tmp_db, capsys):
        rc, out = _run(capsys, [
            "--db", str(tmp_db), "--json", "ingest",
            "--text", "JSON test",
            "--source", "test.py",
        ])
        assert rc == 0
        data = json.loads(out)
        assert data["ok"] is True


class TestQuery:
    def test_query_empty(self, tmp_db, capsys):
        rc, out = _run(capsys, ["--db", str(tmp_db), "query", "anything"])
        assert rc == 0

    def test_query_with_results(self, tmp_db, capsys):
        _ingest(tmp_db)
        rc, out = _run(capsys, ["--db", str(tmp_db), "query", "Test"])
        assert rc == 0


class TestListClaims:
    def test_list_empty(self, tmp_db, capsys):
        rc, out = _run(capsys, ["--db", str(tmp_db), "list-claims"])
        assert rc == 0

    def test_list_with_data(self, tmp_db, capsys):
        _ingest(tmp_db)
        rc, out = _run(capsys, ["--db", str(tmp_db), "list-claims"])
        assert rc == 0
        assert "Test claim" in out

    def test_list_json(self, tmp_db, capsys):
        _ingest(tmp_db)
        rc, out = _run(capsys, ["--db", str(tmp_db), "--json", "list-claims"])
        assert rc == 0
        data = json.loads(out)
        assert data["ok"] is True


class TestListEvents:
    def test_list_events_empty(self, tmp_db, capsys):
        rc, out = _run(capsys, ["--db", str(tmp_db), "list-events"])
        assert rc == 0

    def test_list_events_json(self, tmp_db, capsys):
        _ingest(tmp_db)
        rc, out = _run(capsys, ["--db", str(tmp_db), "--json", "list-events"])
        assert rc == 0
        data = json.loads(out)
        assert data["ok"] is True


class TestRunCycle:
    def test_basic_cycle(self, tmp_db, capsys):
        _ingest(tmp_db)
        rc, out = _run(capsys, ["--db", str(tmp_db), "run-cycle"])
        assert rc == 0

    def test_cycle_with_compact(self, tmp_db, capsys):
        _ingest(tmp_db)
        rc, out = _run(capsys, ["--db", str(tmp_db), "run-cycle", "--with-compact"])
        assert rc == 0

    def test_cycle_json(self, tmp_db, capsys):
        rc, out = _run(capsys, ["--db", str(tmp_db), "--json", "run-cycle"])
        assert rc == 0
        data = json.loads(out)
        assert data["ok"] is True


class TestPin:
    def test_pin_claim(self, tmp_db, capsys):
        _ingest(tmp_db, "Pin target")
        # claim_id=1 for first ingested claim
        rc, out = _run(capsys, ["--db", str(tmp_db), "pin", "1"])
        assert rc == 0
        assert "pinned=1" in out


class TestCompact:
    def test_compact(self, tmp_db, capsys):
        rc, out = _run(capsys, ["--db", str(tmp_db), "compact"])
        assert rc == 0


class TestDedup:
    def test_dedup_empty(self, tmp_db, capsys):
        rc, out = _run(capsys, ["--db", str(tmp_db), "dedup"])
        assert rc == 0

    def test_dedup_dry_run(self, tmp_db, capsys):
        _ingest(tmp_db)
        rc, out = _run(capsys, ["--db", str(tmp_db), "dedup", "--dry-run"])
        assert rc == 0


class TestContext:
    def test_context_empty(self, tmp_db, capsys):
        rc, out = _run(capsys, ["--db", str(tmp_db), "context", "test"])
        assert rc == 0

    def test_context_json(self, tmp_db, capsys):
        _ingest(tmp_db)
        rc, out = _run(capsys, ["--db", str(tmp_db), "--json", "context", "test"])
        assert rc == 0
        data = json.loads(out)
        assert data["ok"] is True


class TestSnapshot:
    def test_snapshot_create(self, tmp_db, capsys):
        rc, out = _run(capsys, ["--db", str(tmp_db), "snapshot"])
        assert rc == 0

    def test_snapshots_list(self, tmp_db, capsys):
        main(["--db", str(tmp_db), "snapshot"])  # create one first
        rc, out = _run(capsys, ["--db", str(tmp_db), "snapshots"])
        assert rc == 0


class TestStealthStatus:
    def test_stealth_status(self, tmp_db, capsys):
        rc, out = _run(capsys, [
            "--db", str(tmp_db),
            "--workspace", str(tmp_db.parent),
            "stealth-status",
        ])
        assert rc == 0
