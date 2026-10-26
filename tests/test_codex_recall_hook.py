"""Codex wire contract using disposable hooks, state and SQLite only."""
import io
import json
import runpy
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "memorymaster/config_templates/hooks"


@pytest.fixture
def installed(tmp_path, monkeypatch):
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.setenv("MEMORYMASTER_RECALL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MEMORYMASTER_HOOK_EVENTS_LOG", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("MEMORYMASTER_PROJECT_PROJECTION_ENABLED", "0")
    for name in ("MEMORYMASTER_INTERNAL_CODEX", "MEMORYMASTER_INTERNAL_RUN"):
        monkeypatch.delenv(name, raising=False)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("memorymaster-codex-recall.py", "memorymaster-recall.py", "memorymaster-session-start.py"):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        (hooks / name).write_text(source.replace("__MEMORYMASTER_PROJECT_ROOT__", tmp_path.as_posix()), encoding="utf-8")
    return hooks


def invoke(hooks, event, data, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
    ns = runpy.run_path(str(hooks / "memorymaster-codex-recall.py"))
    output, errors = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stderr", errors)
    assert ns["main"](event) == 0
    assert errors.getvalue() == ""
    return output.getvalue()


def test_prompt_reuses_claude_core_payload_and_telemetry(installed, tmp_path, monkeypatch):
    from memorymaster.core import hook_log
    from memorymaster.recall import remote

    monkeypatch.setattr(hook_log, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(hook_log, "_LOG_FILE", tmp_path / "hook.log")
    seen = []

    def recall(query, data, *, db_path):
        seen.append((query, data, db_path))
        return "\n- [mm-2bff] governed cited memory\n- [mm-29c2~2] another cited memory", "shared"

    monkeypatch.setattr(remote, "hook_recall", recall)
    data = {"prompt": "explain recall isolation", "cwd": str(tmp_path), "session_id": "codex-session"}
    output = json.loads(invoke(installed, "UserPromptSubmit", data, monkeypatch))
    assert output == {"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "[MemoryMaster recall]\n\n- [mm-2bff] governed cited memory\n- [mm-29c2~2] another cited memory",
    }}
    assert [(query, payload) for query, payload, _ in seen] == [(data["prompt"], data)]
    assert Path(seen[0][2]) == tmp_path / "memorymaster.db"
    record = json.loads((tmp_path / "events.jsonl").read_text())
    assert (record["cli"], record["event"], record["count"]) == ("codex", "UserPromptSubmit", 1)
    assert set(record) == {"ts", "cli", "event", "count"}


def test_session_start_uses_payload_cwd_and_shared_session_core(installed, tmp_path, monkeypatch):
    from memorymaster.core import scope_utils
    from memorymaster.surfaces.setup_hooks import install_codex_recall_hooks

    data_root = tmp_path / "data"
    data_root.mkdir()
    install_codex_recall_hooks(codex_dir=installed.parent, project_root=tmp_path, data_root=data_root)
    monkeypatch.setattr(scope_utils, "scope_from_cwd_or_user", lambda cwd: "project:fixture" if cwd == str(tmp_path) else "wrong")
    with sqlite3.connect(data_root / "memorymaster.db") as db:
        db.execute("CREATE TABLE claims (id INTEGER, text TEXT, claim_type TEXT, status TEXT, created_at TEXT, scope TEXT)")
        db.execute("INSERT INTO claims VALUES (42, 'session memory', 'fact', 'confirmed', '2026-10-26', 'project:fixture')")
    output = json.loads(invoke(installed, "SessionStart", {"cwd": str(tmp_path), "session_id": "s"}, monkeypatch))
    assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "#42 [fact/confirmed] session memory" in output["hookSpecificOutput"]["additionalContext"]
    record = json.loads((tmp_path / "events.jsonl").read_text())
    assert (record["cli"], record["event"], record["count"]) == ("codex", "SessionStart", 1)


@pytest.mark.parametrize("context", ["- #42 [fact/confirmed] session memory", "Compiled profile context"])
def test_session_start_logs_event_after_shared_hook_exit(installed, tmp_path, monkeypatch, context):
    """A successful shared SessionStart exits before returning to the adapter."""
    payload = {"hookSpecificOutput": {
        "hookEventName": "SessionStart", "additionalContext": context,
    }}
    (installed / "memorymaster-session-start.py").write_text(
        "import sys\nprint(" + repr(json.dumps(payload)) + ")\nsys.exit(0)\n",
        encoding="utf-8",
    )
    assert json.loads(invoke(installed, "SessionStart", {"cwd": str(tmp_path)}, monkeypatch)) == payload
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert (rows[0]["cli"], rows[0]["event"], rows[0]["count"]) == ("codex", "SessionStart", 1)


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
@pytest.mark.parametrize("source", ["", "raise RuntimeError('private error')", "import sys; sys.exit(2)"])
def test_empty_and_errors_are_silent(installed, tmp_path, monkeypatch, event, source):
    name = "memorymaster-session-start.py" if event == "SessionStart" else "memorymaster-recall.py"
    (installed / name).write_text(source)
    assert invoke(installed, event, {"cwd": str(tmp_path)}, monkeypatch) == ""
    assert not (tmp_path / "events.jsonl").exists()


@pytest.mark.parametrize("marker", ["MEMORYMASTER_INTERNAL_CODEX", "MEMORYMASTER_INTERNAL_RUN"])
@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
def test_internal_runs_skip_before_recall(installed, tmp_path, monkeypatch, marker, event):
    monkeypatch.setenv(marker, "1")
    for hook in installed.glob("*.py"):
        if hook.name != "memorymaster-codex-recall.py":
            hook.write_text("raise AssertionError('must never run')")
    assert invoke(installed, event, {"cwd": str(tmp_path)}, monkeypatch) == ""
    assert not (tmp_path / "events.jsonl").exists()


def test_output_cap_and_telemetry_failure_preserve_json(installed, tmp_path, monkeypatch):
    context = "\n- [mm-42] " + "🧠" * 5000
    payload = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
    (installed / "memorymaster-recall.py").write_text("print(" + repr(json.dumps(payload)) + ")", encoding="utf-8")
    monkeypatch.setenv("MEMORYMASTER_HOOK_EVENTS_LOG", str(tmp_path))  # unwritable as a file
    output = json.loads(invoke(installed, "UserPromptSubmit", {"cwd": str(tmp_path)}, monkeypatch))
    text = output["hookSpecificOutput"]["additionalContext"]
    assert text.startswith("\n- [mm-42]")
    assert 0 < len(text.encode("utf-8")) <= 1800


@pytest.mark.parametrize("stdin", ["not json", "[]", '{"cwd": 42}', '{"prompt": []}', "null"])
def test_real_process_bad_input_exits_zero(installed, stdin):
    result = subprocess.run([sys.executable, str(installed / "memorymaster-codex-recall.py"), "--event", "UserPromptSubmit"],
                            input=stdin, capture_output=True, text=True, timeout=15)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
def test_real_process_emits_only_codex_json(installed, tmp_path, event):
    payload = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": "- [mm-42] fixture"}}
    name = "memorymaster-session-start.py" if event == "SessionStart" else "memorymaster-recall.py"
    (installed / name).write_text("print(" + repr(json.dumps(payload)) + ")")
    result = subprocess.run([sys.executable, str(installed / "memorymaster-codex-recall.py"), "--event", event],
                            input=json.dumps({"cwd": str(tmp_path), "session_id": "fixture", "prompt": "test memory"}),
                            capture_output=True, text=True, timeout=15)
    assert (result.returncode, result.stderr) == (0, "")
    assert json.loads(result.stdout) == payload
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert (rows[0]["cli"], rows[0]["event"], rows[0]["count"]) == ("codex", event, 1)
