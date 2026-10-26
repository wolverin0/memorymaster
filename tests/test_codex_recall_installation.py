"""Hermetic tests for the parameterized Codex recall hook installer."""

import ast
import json
from pathlib import Path

import pytest

from memorymaster.surfaces import setup_hooks


def test_install_codex_recall_hooks_separates_code_and_data_roots(tmp_path):
    codex_dir = tmp_path / "codex"
    code_root = tmp_path / "hooks worktree"
    data_root = tmp_path / "main checkout"

    setup_hooks.install_codex_recall_hooks(
        codex_dir=codex_dir, project_root=code_root, data_root=data_root,
    )

    for name, variable, expected_root in (
        ("memorymaster-session-start.py", "MM_ROOT", data_root),
        ("memorymaster-recall.py", "PROJECT_ROOT", code_root),
        ("memorymaster-codex-recall.py", "PROJECT_ROOT", code_root),
    ):
        source = (codex_dir / "hooks" / name).read_text(encoding="utf-8")
        assignment = next(
            node for node in ast.parse(source).body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == variable for target in node.targets)
        )
        value = assignment.value
        if isinstance(value, ast.Call):  # MM_ROOT wraps its string in Path().
            value = value.args[0]
        assert ast.literal_eval(value) == expected_root.as_posix()
        assert "__MEMORYMASTER_PROJECT_ROOT__" not in source


def _template_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    templates = tmp_path / "templates" / "hooks"
    templates.mkdir(parents=True)
    for name, body in {
        "memorymaster-codex-recall.py": "ROOT = '__MEMORYMASTER_PROJECT_ROOT__'\n",
        "memorymaster-session-start.py": "ROOT = '__MEMORYMASTER_PROJECT_ROOT__'\n",
        "memorymaster-recall.py": "ROOT = '__MEMORYMASTER_PROJECT_ROOT__'\n",
    }.items():
        (templates / name).write_text(body, encoding="utf-8")
    monkeypatch.setattr(setup_hooks, "TEMPLATES_DIR", templates.parent)
    return templates


def test_install_codex_recall_hooks_merges_events_and_is_idempotent(tmp_path, monkeypatch):
    _template_dir(tmp_path, monkeypatch)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    hooks_path = codex_dir / "hooks.json"
    hooks_path.write_text(
        json.dumps(
            {
                "other": {"keep": True},
                "hooks": {
                    "Stop": [{"hooks": [{"type": "command", "command": "keep-stop"}]}],
                    "SessionStart": [{"hooks": [{"type": "command", "command": "keep-start"}]}],
                },
            }
        ),
        encoding="utf-8",
    )

    setup_hooks.install_codex_recall_hooks(codex_dir=codex_dir, project_root=tmp_path / "project")
    setup_hooks.install_codex_recall_hooks(codex_dir=codex_dir, project_root=tmp_path / "project")

    config = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert config["other"] == {"keep": True}
    assert len(config["hooks"]["Stop"]) == 1
    for event in ("SessionStart", "UserPromptSubmit"):
        entries = config["hooks"][event]
        assert len(entries) == 2 if event == "SessionStart" else len(entries) == 1
        installed = [entry for entry in entries if "memorymaster-codex-recall.py" in json.dumps(entry)]
        assert len(installed) == 1
        command = installed[0]["hooks"][0]
        assert command["command"] == command["commandWindows"]
        assert f'--event {event}' in command["command"]

    expected = f"ROOT = '{str(tmp_path / 'project').replace(chr(92), '/')}'\n"
    for name in (
        "memorymaster-codex-recall.py",
        "memorymaster-session-start.py",
        "memorymaster-recall.py",
    ):
        assert (codex_dir / "hooks" / name).read_text(encoding="utf-8") == expected


def test_install_codex_recall_hooks_malformed_config_keeps_backup(tmp_path, monkeypatch):
    _template_dir(tmp_path, monkeypatch)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    hooks_path = codex_dir / "hooks.json"
    hooks_path.write_text("{ malformed", encoding="utf-8")

    setup_hooks.install_codex_recall_hooks(codex_dir=codex_dir)

    backups = list(codex_dir.glob("hooks.json.corrupt-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{ malformed"


def test_update_keeps_peer_commands_in_same_entry(tmp_path, monkeypatch):
    _template_dir(tmp_path, monkeypatch)
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir()
    peer = {"type": "command", "command": "peer-hook", "timeout": 7}
    path = codex_dir / "hooks.json"
    path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{
        "matcher": "keep-matcher", "hooks": [peer, {
            "type": "command", "command": "python memorymaster-codex-recall.py --event UserPromptSubmit",
        }],
    }]}}))
    setup_hooks.install_codex_recall_hooks(codex_dir=codex_dir)
    entries = json.loads(path.read_text())["hooks"]["UserPromptSubmit"]
    assert entries[0] == {"matcher": "keep-matcher", "hooks": [peer]}
    assert len(entries) == 2
