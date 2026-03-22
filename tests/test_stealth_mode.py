"""Tests for stealth mode (--stealth) DB path routing."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from memorymaster.cli import STEALTH_DB_NAME, main


@pytest.fixture()
def stealth_cwd(tmp_path, monkeypatch):
    """Switch cwd to a temporary directory for stealth tests."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestStealthResolve:
    """Unit tests for _resolve_db_path and _stealth_active."""

    def test_stealth_flag_creates_db_in_cwd(self, stealth_cwd):
        rc = main(["--stealth", "init-db"])
        assert rc == 0
        assert (stealth_cwd / STEALTH_DB_NAME).exists()

    def test_stealth_flag_does_not_touch_default(self, stealth_cwd):
        main(["--stealth", "init-db"])
        assert not (stealth_cwd / "memorymaster.db").exists()

    def test_auto_detect_stealth_db(self, stealth_cwd, capsys):
        """When stealth DB exists in cwd and --db is not overridden, use it."""
        # First create the stealth DB.
        main(["--stealth", "init-db"])
        capsys.readouterr()  # discard init-db output
        # Now without --stealth, it should auto-detect.
        rc = main(["--json", "stealth-status"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["data"]["stealth_active"] is True

    def test_explicit_db_overrides_stealth(self, stealth_cwd):
        """--db takes priority over auto-detect."""
        # Create stealth DB in cwd.
        main(["--stealth", "init-db"])
        custom = str(stealth_cwd / "custom.db")
        rc = main(["--db", custom, "init-db"])
        assert rc == 0
        assert Path(custom).exists()


class TestStealthStatus:
    def test_status_inactive(self, stealth_cwd, capsys):
        rc = main(["stealth-status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "inactive" in out

    def test_status_active_via_flag(self, stealth_cwd, capsys):
        rc = main(["--stealth", "stealth-status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "ACTIVE" in out

    def test_status_active_auto_detect(self, stealth_cwd, capsys):
        main(["--stealth", "init-db"])
        capsys.readouterr()  # discard init-db output
        rc = main(["stealth-status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "ACTIVE" in out

    def test_status_json(self, stealth_cwd, capsys):
        main(["--stealth", "init-db"])
        capsys.readouterr()  # discard init-db output
        rc = main(["--json", "stealth-status"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["data"]["stealth_active"] is True
        assert data["data"]["stealth_db_exists"] is True
        assert STEALTH_DB_NAME in data["data"]["stealth_db_path"]

    def test_status_json_inactive(self, stealth_cwd, capsys):
        rc = main(["--json", "stealth-status"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["data"]["stealth_active"] is False


class TestStealthIngestQuery:
    """Verify that ingest/query use the stealth DB when active."""

    def test_ingest_into_stealth(self, stealth_cwd, capsys):
        main(["--stealth", "init-db"])
        capsys.readouterr()
        rc = main([
            "--stealth", "ingest",
            "--text", "stealth claim",
            "--source", "test|loc|excerpt",
        ])
        assert rc == 0
        capsys.readouterr()
        # Query it back.
        rc = main(["--stealth", "--json", "list-claims"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        claims = data["data"]
        assert any("stealth claim" in c["text"] for c in claims)

    def test_stealth_isolation(self, stealth_cwd, capsys):
        """Claims in stealth DB don't appear in default DB and vice versa."""
        # Use absolute path to avoid auto-detect (which triggers when --db
        # matches the default "memorymaster.db" and a stealth DB exists).
        default_db = str(stealth_cwd / "default.db")
        # Init both DBs.
        main(["--stealth", "init-db"])
        main(["--db", default_db, "init-db"])
        capsys.readouterr()
        # Ingest into stealth.
        main(["--stealth", "ingest", "--text", "only-in-stealth", "--source", "s|l|e"])
        # Ingest into default.
        main(["--db", default_db, "ingest", "--text", "only-in-default", "--source", "s|l|e"])
        capsys.readouterr()
        # Check stealth has only stealth claim.
        main(["--stealth", "--json", "list-claims"])
        data = json.loads(capsys.readouterr().out)
        texts = [c["text"] for c in data["data"]]
        assert "only-in-stealth" in texts
        assert "only-in-default" not in texts
        # Check default has only default claim.
        main(["--db", default_db, "--json", "list-claims"])
        data = json.loads(capsys.readouterr().out)
        texts = [c["text"] for c in data["data"]]
        assert "only-in-default" in texts
        assert "only-in-stealth" not in texts
