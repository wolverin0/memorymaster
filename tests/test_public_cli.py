from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from memorymaster.surfaces.cli import main
from memorymaster.surfaces.cli_handlers_public import _emit


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    db = tmp_path / "cli.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_ROOTS", f"cli={workspace}")
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_TRUST_MODE", "local-trusted")
    return db, workspace


def _base(db: Path, workspace: Path) -> list[str]:
    return ["--json", "--db", str(db), "--workspace", str(workspace)]


def test_cli_remember_recall_forget_improve_contract(cli_env, capsys) -> None:
    db, workspace = cli_env
    assert main([*_base(db, workspace), "remember", "--text", "Captured CLI note."]) == 0
    remembered = json.loads(capsys.readouterr().out)
    assert remembered["api_version"] == "memorymaster.public.v1"
    source_id = remembered["source_item"]["id"]

    assert main([*_base(db, workspace), "recall", "Captured CLI note."]) == 0
    recalled = json.loads(capsys.readouterr().out)
    assert recalled["api_version"] == "memorymaster.public.v1"
    assert recalled["trust_mode"] == "trusted"

    assert main(
        [*_base(db, workspace), "forget", "--source-item-id", str(source_id)]
    ) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["apply"] is False
    assert preview["evidence_preserved"] is True

    assert main([*_base(db, workspace), "improve"]) == 0
    improved = json.loads(capsys.readouterr().out)
    assert improved["api_version"] == "memorymaster.public.v1"


def test_cli_remember_requires_exactly_one_input(cli_env) -> None:
    db, workspace = cli_env
    with pytest.raises(SystemExit) as caught:
        main([*_base(db, workspace), "remember"])
    assert caught.value.code == 2


def test_cli_url_capture_is_awaiting_evidence(cli_env, capsys) -> None:
    db, workspace = cli_env
    assert main(
        [
            *_base(db, workspace),
            "remember",
            "--url",
            "https://example.com/reference",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["warnings"] == ["awaiting_evidence"]


def test_public_json_output_is_safe_for_windows_legacy_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _LegacyWindowsStream()
    monkeypatch.setattr(sys, "stdout", stream)

    _emit({"comparison": "candidate score ≥ threshold"}, json_output=True)

    assert json.loads(stream.text)["comparison"].endswith("≥ threshold")


class _LegacyWindowsStream:
    def __init__(self) -> None:
        self.fragments: list[str] = []

    @property
    def text(self) -> str:
        return "".join(self.fragments)

    def write(self, value: str) -> int:
        value.encode("cp1252", errors="strict")
        self.fragments.append(value)
        return len(value)

    def flush(self) -> None:
        return None
