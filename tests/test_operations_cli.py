from __future__ import annotations

import json
from pathlib import Path

from memorymaster.core.service import MemoryService
from memorymaster.surfaces.operations import main


def test_privacy_cli_is_dry_run_only(tmp_path: Path, capsys) -> None:
    db = tmp_path / "ops.db"
    service = MemoryService(db, workspace_root=tmp_path)
    service.init_db()

    code = main([
        "privacy-plan",
        "--db",
        str(db),
        "--workspace",
        str(tmp_path),
        "--principal",
        "operator-a",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["dry_run"] is True
    assert payload["mutation_count"] == 0


def test_backup_cli_refuses_command_line_key_and_missing_secret(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("MEMORYMASTER_BACKUP_KEY", raising=False)
    code = main([
        "backup-create",
        "--db",
        str(tmp_path / "unused.db"),
        "--destination",
        str(tmp_path / "unused.enc"),
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert "MEMORYMASTER_BACKUP_KEY" in payload["error"]
