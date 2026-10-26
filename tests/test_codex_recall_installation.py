"""Hermetic tests for the parameterized Codex recall hook installer."""

import ast
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

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
        assert set(command) == {"type", "command", "commandWindows", "timeout"}
        assert command["commandWindows"] == command["command"]
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


@pytest.mark.parametrize("python_exe", ["C:/Python/python.exe", "C:/Program Files/Python/python.exe"])
def test_windows_recall_command_does_not_start_with_quoted_executable(tmp_path, monkeypatch, python_exe):
    _template_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(setup_hooks, "IS_WINDOWS", True)
    monkeypatch.setattr(setup_hooks, "PYTHON_EXE", python_exe.replace("/", "\\"))
    target = tmp_path / "config with spaces"
    setup_hooks.install_codex_recall_hooks(codex_dir=target)
    config = json.loads((target / "hooks.json").read_text())
    for event in ("SessionStart", "UserPromptSubmit"):
        hook = config["hooks"][event][0]["hooks"][0]
        command = hook["command"]
        assert not command.startswith('"')
        assert "\\" not in command
        assert python_exe in command
        assert hook["commandWindows"] == command
        assert f"--event {event}" in command


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows hook shell")
def test_windows_installed_command_preserves_stdin_and_spaced_paths(tmp_path, monkeypatch):
    target = tmp_path / "config with spaces"
    setup_hooks.install_codex_recall_hooks(codex_dir=target)
    # Exercise the installed command through cmd.exe, as Codex does, without
    # invoking recall or touching any memory store.
    adapter = target / "hooks" / "memorymaster-codex-recall.py"
    adapter.write_text("import sys\nprint(sys.stdin.read() + '|' + '|'.join(sys.argv[1:]))\n")
    config = json.loads((target / "hooks.json").read_text())
    command = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["commandWindows"]
    # A raw command line matches Rust's raw_arg(command_line); passing a Python
    # argv list would add CRT escaping that cmd.exe does not understand.
    result = subprocess.run('cmd.exe /C "' + command + '"', input='{"prompt":"probe"}',
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '{"prompt":"probe"}|--event|UserPromptSubmit'


def test_windows_recall_command_uses_bare_forward_slash_paths_without_spaces(monkeypatch):
    monkeypatch.setattr(setup_hooks, "IS_WINDOWS", True)
    command = setup_hooks._codex_recall_command(
        r"C:\Python\python.exe", Path("C:/hooks/memorymaster-codex-recall.py"), "SessionStart",
    )
    assert command == "C:/Python/python.exe C:/hooks/memorymaster-codex-recall.py --event SessionStart"


def test_windows_recall_command_wraps_unspaced_shell_metacharacters(monkeypatch):
    monkeypatch.setattr(setup_hooks, "IS_WINDOWS", True)
    command = setup_hooks._codex_recall_command(
        "C:/Python/python.exe", Path("C:/hooks/memorymaster&recall.py"), "SessionStart",
    )
    assert command == (
        'cmd.exe /d /v:off /s /c ""C:/Python/python.exe" '
        '"C:/hooks/memorymaster^&recall.py" --event SessionStart"'
    )


def test_windows_recall_command_rejects_percent_expansion_paths(monkeypatch):
    monkeypatch.setattr(setup_hooks, "IS_WINDOWS", True)
    with pytest.raises(ValueError, match="cannot contain percent signs"):
        setup_hooks._codex_recall_command(
            "C:/Python/python.exe", Path("C:/hooks/memorymaster%TEMP%.py"), "SessionStart",
        )


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows hook shell")
def test_windows_installed_command_preserves_stdin_with_unspaced_ampersand_path(monkeypatch):
    root = Path(tempfile.gettempdir()) / f"mm-t1806-{uuid.uuid4().hex}"
    root.mkdir(mode=0o777)
    adapter = root / "memorymaster&recall.py"
    adapter.write_text(
        "import sys\nprint(sys.stdin.read() + '|' + '|'.join(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(setup_hooks, "IS_WINDOWS", True)
    command = setup_hooks._codex_recall_command(sys.executable, adapter, "UserPromptSubmit")
    try:
        result = subprocess.run(
            'cmd.exe /C "' + command + '"', input='{"prompt":"probe"}',
            text=True, capture_output=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == '{"prompt":"probe"}|--event|UserPromptSubmit'
    finally:
        adapter.unlink(missing_ok=True)
        root.rmdir()
