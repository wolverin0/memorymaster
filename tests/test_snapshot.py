"""Tests for git-backed DB versioning (P4 feature #25)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from memorymaster.cli import main
from memorymaster.snapshot import (
    SnapshotDiff,
    SnapshotInfo,
    create_snapshot,
    diff_snapshot,
    install_git_hook,
    list_snapshots,
    rollback,
)


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    """Create and initialise a fresh test DB."""
    db = tmp_path / "snap_test.db"
    main(["--db", str(db), "init-db"])
    return db


@pytest.fixture()
def populated_db(tmp_db: Path) -> Path:
    """DB with a couple of claims already ingested."""
    main(["--db", str(tmp_db), "ingest", "--text", "Claim Alpha", "--source", "src_a"])
    main(["--db", str(tmp_db), "ingest", "--text", "Claim Beta", "--source", "src_b"])
    return tmp_db


def _capture(capsys, argv: list[str]) -> dict:
    """Run CLI with --json and return parsed JSON output."""
    capsys.readouterr()
    rc = main(argv)
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    return {"rc": rc, **parsed}


# ---------------------------------------------------------------------------
# Core snapshot module tests
# ---------------------------------------------------------------------------


class TestCreateSnapshot:
    def test_creates_snapshot_file(self, populated_db: Path) -> None:
        info = create_snapshot(populated_db, message="test snap")
        assert isinstance(info, SnapshotInfo)
        assert Path(info.path).exists()
        assert info.size_bytes > 0
        assert info.message == "test snap"
        assert "nogit" in info.snapshot_id  # no git repo in tmp_path

    def test_snapshot_is_valid_sqlite(self, populated_db: Path) -> None:
        info = create_snapshot(populated_db)
        conn = sqlite3.connect(info.path)
        rows = conn.execute("SELECT count(*) FROM claims").fetchone()
        conn.close()
        assert rows[0] == 2

    def test_meta_sidecar_written(self, populated_db: Path) -> None:
        info = create_snapshot(populated_db, message="hello")
        meta_path = Path(info.path).with_suffix(".meta")
        assert meta_path.exists()
        content = meta_path.read_text(encoding="utf-8")
        assert "message=hello" in content

    def test_missing_db_raises(self, tmp_path: Path) -> None:
        fake = tmp_path / "nonexistent.db"
        with pytest.raises(FileNotFoundError):
            create_snapshot(fake)

    @patch("memorymaster.snapshot.get_git_head", return_value="a" * 40)
    def test_snapshot_with_git_hash(self, mock_git, populated_db: Path) -> None:
        info = create_snapshot(populated_db)
        assert info.commit_hash == "a" * 40
        assert info.snapshot_id.startswith("aaaaaaaa_")


class TestListSnapshots:
    def test_empty_when_none(self, populated_db: Path) -> None:
        assert list_snapshots(populated_db) == []

    def test_lists_created_snapshots(self, populated_db: Path) -> None:
        create_snapshot(populated_db, message="first")
        create_snapshot(populated_db, message="second")
        snaps = list_snapshots(populated_db)
        assert len(snaps) == 2
        # newest first
        assert snaps[0].message == "second"
        assert snaps[1].message == "first"


class TestRollback:
    def test_rollback_restores_state(self, populated_db: Path) -> None:
        info = create_snapshot(populated_db, message="before third")
        # Add a third claim
        main(["--db", str(populated_db), "ingest", "--text", "Claim Gamma", "--source", "src_c"])
        conn = sqlite3.connect(str(populated_db))
        assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 3
        conn.close()

        # Rollback
        rollback(populated_db, info.snapshot_id)
        conn = sqlite3.connect(str(populated_db))
        assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 2
        conn.close()

    def test_rollback_creates_safety_backup(self, populated_db: Path) -> None:
        info = create_snapshot(populated_db)
        main(["--db", str(populated_db), "ingest", "--text", "Extra", "--source", "s"])
        rollback(populated_db, info.snapshot_id)
        snaps = list_snapshots(populated_db)
        # Original + pre-rollback safety backup
        assert len(snaps) >= 2
        messages = [s.message for s in snaps]
        assert any("pre-rollback" in m for m in messages)

    def test_rollback_unknown_id_raises(self, populated_db: Path) -> None:
        with pytest.raises(FileNotFoundError):
            rollback(populated_db, "nonexistent_12345678")


class TestDiffSnapshot:
    def test_diff_no_changes(self, populated_db: Path) -> None:
        info = create_snapshot(populated_db)
        result = diff_snapshot(populated_db, info.snapshot_id)
        assert isinstance(result, SnapshotDiff)
        assert result.summary["added"] == 0
        assert result.summary["removed"] == 0
        assert result.summary["changed"] == 0

    def test_diff_detects_added(self, populated_db: Path) -> None:
        info = create_snapshot(populated_db)
        main(["--db", str(populated_db), "ingest", "--text", "New claim", "--source", "s"])
        result = diff_snapshot(populated_db, info.snapshot_id)
        assert result.summary["added"] == 1
        assert result.added[0]["text"] == "New claim"

    def test_diff_detects_status_change(self, populated_db: Path) -> None:
        info = create_snapshot(populated_db)
        # Directly change a claim status to simulate confirmation
        conn = sqlite3.connect(str(populated_db))
        conn.execute("UPDATE claims SET status = 'confirmed' WHERE id = 1")
        conn.commit()
        conn.close()
        result = diff_snapshot(populated_db, info.snapshot_id)
        assert result.summary["changed"] == 1
        assert result.changed[0]["changes"]["status"]["old"] == "candidate"
        assert result.changed[0]["changes"]["status"]["new"] == "confirmed"


class TestInstallGitHook:
    def test_install_creates_hook(self, tmp_path: Path) -> None:
        git_dir = tmp_path / ".git" / "hooks"
        git_dir.mkdir(parents=True)
        # Create minimal .git dir
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        result = install_git_hook(tmp_path)
        assert result["installed"] is True
        hook_path = Path(result["path"])
        assert hook_path.exists()
        assert "memorymaster" in hook_path.read_text(encoding="utf-8")

    def test_install_appends_to_existing(self, tmp_path: Path) -> None:
        git_dir = tmp_path / ".git" / "hooks"
        git_dir.mkdir(parents=True)
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        hook_path = git_dir / "post-commit"
        hook_path.write_text("#!/bin/sh\necho existing\n", encoding="utf-8")
        result = install_git_hook(tmp_path)
        assert result["installed"] is True
        assert result["appended"] is True
        content = hook_path.read_text(encoding="utf-8")
        assert "existing" in content
        assert "memorymaster" in content

    def test_install_skips_if_already_present(self, tmp_path: Path) -> None:
        git_dir = tmp_path / ".git" / "hooks"
        git_dir.mkdir(parents=True)
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        hook_path = git_dir / "post-commit"
        hook_path.write_text("#!/bin/sh\n# memorymaster already here\n", encoding="utf-8")
        result = install_git_hook(tmp_path)
        assert result["installed"] is False

    def test_install_no_git_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            install_git_hook(tmp_path)


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestSnapshotCLI:
    def test_snapshot_command_json(self, populated_db: Path, capsys) -> None:
        result = _capture(capsys, [
            "--json", "--db", str(populated_db), "snapshot", "-m", "cli test",
        ])
        assert result["rc"] == 0
        assert result["ok"] is True
        assert result["data"]["message"] == "cli test"
        assert result["data"]["size_bytes"] > 0
        assert "snapshot_id" in result["data"]

    def test_snapshot_command_text(self, populated_db: Path, capsys) -> None:
        capsys.readouterr()
        rc = main(["--db", str(populated_db), "snapshot", "-m", "hello"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "snapshot created:" in out
        assert "hello" in out

    def test_snapshots_command_json(self, populated_db: Path, capsys) -> None:
        main(["--db", str(populated_db), "snapshot"])
        main(["--db", str(populated_db), "snapshot", "-m", "second"])
        result = _capture(capsys, ["--json", "--db", str(populated_db), "snapshots"])
        assert result["rc"] == 0
        assert result["ok"] is True
        assert result["meta"]["total"] == 2

    def test_snapshots_empty(self, populated_db: Path, capsys) -> None:
        capsys.readouterr()
        rc = main(["--db", str(populated_db), "snapshots"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "no snapshots found" in out

    def test_rollback_command_json(self, populated_db: Path, capsys) -> None:
        # Create snapshot, add claim, rollback
        main(["--db", str(populated_db), "snapshot", "-m", "base"])
        capsys.readouterr()
        result_snap = _capture(capsys, [
            "--json", "--db", str(populated_db), "snapshots",
        ])
        snap_id = result_snap["data"][0]["snapshot_id"]

        main(["--db", str(populated_db), "ingest", "--text", "Extra", "--source", "s"])

        result = _capture(capsys, [
            "--json", "--db", str(populated_db), "rollback", snap_id, "--yes",
        ])
        assert result["rc"] == 0
        assert result["ok"] is True
        assert result["data"]["restored_snapshot_id"] == snap_id

    def test_diff_command_json(self, populated_db: Path, capsys) -> None:
        main(["--db", str(populated_db), "snapshot", "-m", "base"])
        capsys.readouterr()
        result_snap = _capture(capsys, [
            "--json", "--db", str(populated_db), "snapshots",
        ])
        snap_id = result_snap["data"][0]["snapshot_id"]

        main(["--db", str(populated_db), "ingest", "--text", "New one", "--source", "s"])

        result = _capture(capsys, [
            "--json", "--db", str(populated_db), "diff", snap_id,
        ])
        assert result["rc"] == 0
        assert result["ok"] is True
        assert result["data"]["summary"]["added"] == 1

    def test_diff_command_text(self, populated_db: Path, capsys) -> None:
        main(["--db", str(populated_db), "snapshot"])
        capsys.readouterr()
        snap_result = _capture(capsys, [
            "--json", "--db", str(populated_db), "snapshots",
        ])
        snap_id = snap_result["data"][0]["snapshot_id"]

        main(["--db", str(populated_db), "ingest", "--text", "Another one", "--source", "s"])
        capsys.readouterr()
        rc = main(["--db", str(populated_db), "diff", snap_id])
        out = capsys.readouterr().out
        assert rc == 0
        assert "+1 added" in out

    def test_install_hook_command(self, tmp_path: Path, capsys) -> None:
        db = tmp_path / "hook_test.db"
        main(["--db", str(db), "init-db"])
        git_dir = tmp_path / ".git" / "hooks"
        git_dir.mkdir(parents=True)
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

        result = _capture(capsys, [
            "--json", "--db", str(db), "--workspace", str(tmp_path), "install-hook",
        ])
        assert result["rc"] == 0
        assert result["ok"] is True
        assert result["data"]["installed"] is True
