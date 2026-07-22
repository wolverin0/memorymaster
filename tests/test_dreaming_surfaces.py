from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.surfaces import dreaming_cli, mcp_server, setup_hooks
from memorymaster.surfaces.cli import COMMAND_HANDLERS, build_parser, main


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "memorymaster" / "config_templates" / "hooks" / "memorymaster-dream-capture.py"


def test_cli_and_mcp_expose_only_explicit_run_and_read_only_status() -> None:
    run_args = build_parser().parse_args(["dream-run", "--apply-candidates", "--max-sessions", "4"])
    status_args = build_parser().parse_args(["dream-status"])

    assert run_args.command == "dream-run"
    assert run_args.apply_candidates is True
    assert run_args.max_sessions == 4
    assert status_args.command == "dream-status"
    assert {"dream-run", "dream-status"} <= COMMAND_HANDLERS.keys()
    assert mcp_server.MCP_TOOL_POLICIES["dream_status"].action == "query"


def test_dream_status_json_never_contains_transcript_text(tmp_path: Path, monkeypatch, capsys) -> None:
    state_db = tmp_path / "capture.db"
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_STATE_DB", str(state_db))

    assert main(["--json", "dream-status"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    assert "messages" not in json.dumps(payload)
    assert not state_db.exists()


def test_dream_run_returns_failure_exit_for_partial_result(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_STATE_DB", str(tmp_path / "capture.db"))

    class PartialWorker:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, **kwargs):
            return {"ok": True, "errors": 2, "extracted": 0, "consolidated": 0}

    monkeypatch.setattr(dreaming_cli, "DreamWorker", PartialWorker)
    args = SimpleNamespace(
        apply_candidates=False, scope=None, max_sessions=1, json_output=True,
    )

    assert dreaming_cli.handle_dream_run(args, object(), None, None) == 1


def test_setup_parser_has_explicit_shadow_and_activation_flags() -> None:
    shadow = setup_hooks.build_arg_parser().parse_args(["--enable-dream"])
    active = setup_hooks.build_arg_parser().parse_args(["--enable-dream", "--dream-apply-candidates"])

    assert shadow.enable_dream and not shadow.dream_apply_candidates
    assert active.enable_dream and active.dream_apply_candidates
    assert TEMPLATE.is_file()


def test_hook_template_is_quiet_and_redacts_before_queue(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    rendered = tmp_path / "hook.py"
    rendered.write_text(
        TEMPLATE.read_text(encoding="utf-8").replace("__MEMORYMASTER_PROJECT_ROOT__", str(ROOT).replace("\\", "/")),
        encoding="utf-8",
    )
    secret = "sk-LiveSecret1234567890abcd"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"message": {"role": "user", "content": f"Please remember {secret}"}}) + "\n" +
        json.dumps({"message": {"role": "assistant", "content": "I will remember the safe preference only."}}) + "\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["MEMORYMASTER_CAPTURE_STATE_DB"] = str(tmp_path / "capture.db")
    payload = {"session_id": "hook-session", "transcript_path": str(transcript), "cwd": str(project)}

    proc = subprocess.run(
        [sys.executable, str(rendered), "--provider", "claude"],
        input=json.dumps(payload), text=True, capture_output=True, env=env, timeout=10,
    )

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""
    on_disk = (tmp_path / "capture.db").read_bytes()
    assert secret.encode() not in on_disk


def test_hook_template_captures_codex_stop_payload(tmp_path: Path) -> None:
    rendered = tmp_path / "hook.py"
    rendered.write_text(
        TEMPLATE.read_text(encoding="utf-8").replace(
            "__MEMORYMASTER_PROJECT_ROOT__", str(ROOT).replace("\\", "/"),
        ),
        encoding="utf-8",
    )
    transcript = tmp_path / "codex.jsonl"
    rows = [
        {
            "type": "response_item",
            "timestamp": "2026-07-22T10:00:00Z",
            "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "I prefer concise reports."}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-07-22T10:01:00Z",
            "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "I will keep reports concise."}],
            },
        },
    ]
    transcript.write_text("\n".join(map(json.dumps, rows)) + "\n", encoding="utf-8")
    state_db = tmp_path / "capture.db"
    env = {**os.environ, "PYTHONPATH": str(ROOT), "MEMORYMASTER_CAPTURE_STATE_DB": str(state_db)}
    payload = {
        "session_id": "codex-hook-session",
        "transcript_path": str(transcript),
        "cwd": str(tmp_path / "memorymaster"),
    }

    proc = subprocess.run(
        [sys.executable, str(rendered), "--provider", "codex"],
        input=json.dumps(payload), text=True, capture_output=True, env=env, timeout=10,
    )

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""
    assert DreamLedger.read_status(state_db)["queue"] == {"captured": 1}


def test_installer_preserves_existing_hooks_and_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    claude = tmp_path / ".claude"
    codex = tmp_path / ".codex"
    claude.mkdir()
    codex.mkdir()
    (claude / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep-me"}]}]}}), encoding="utf-8")
    (codex / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep-codex"}]}]}}), encoding="utf-8")
    monkeypatch.setattr(setup_hooks, "HOME", tmp_path)
    monkeypatch.setattr(setup_hooks, "CLAUDE_DIR", claude)
    monkeypatch.setattr(setup_hooks, "CODEX_DIR", codex)

    setup_hooks.install_dream_hooks(install_claude=True, install_codex=True)
    setup_hooks.install_dream_hooks(install_claude=True, install_codex=True)

    claude_text = (claude / "settings.json").read_text(encoding="utf-8")
    codex_text = (codex / "hooks.json").read_text(encoding="utf-8")
    assert claude_text.count("memorymaster-dream-capture.py") == 3
    assert codex_text.count("memorymaster-dream-capture.py") == 2  # command + commandWindows
    assert "keep-me" in claude_text
    assert "keep-codex" in codex_text


def test_verify_only_enable_dream_reports_readiness_without_writes(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    claude = tmp_path / ".claude"
    codex = tmp_path / ".codex"
    monkeypatch.setattr(setup_hooks, "HOME", tmp_path)
    monkeypatch.setattr(setup_hooks, "CLAUDE_DIR", claude)
    monkeypatch.setattr(setup_hooks, "CODEX_DIR", codex)
    monkeypatch.setattr(setup_hooks, "IS_WINDOWS", False)
    monkeypatch.setattr(
        setup_hooks,
        "verify_install",
        lambda _db: {"status": "PASS", "detail": "temporary database verified"},
    )

    assert setup_hooks.main([
        "--json", "--verify-only", "--enable-dream", "--db", str(tmp_path / "verify.db")
    ]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["dream"]["status"] == "BLOCKED"
    assert payload["dream"]["mode"] == "shadow"
    assert not claude.exists()
    assert not codex.exists()
    assert not (tmp_path / ".memorymaster").exists()
