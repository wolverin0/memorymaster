from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from memorymaster.surfaces import agy_hooks as hooks


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.delenv(hooks.INTERNAL_ENV, raising=False)
    monkeypatch.delenv("AGY_BATCH_PROFILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MEMORYMASTER_HOOK_EVENTS_LOG", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("MEMORYMASTER_CAPTURE_STATE_DB", str(tmp_path / "capture.db"))
    monkeypatch.setenv("MEMORYMASTER_SCOPE_ALIASES", str(tmp_path / "aliases.json"))
    monkeypatch.setenv("MEMORYMASTER_DREAM_ENABLED", "1")


@pytest.fixture
def payload(tmp_path):
    return {"conversationId": "fixture-session", "workspacePaths": [str(tmp_path / "fixture")],
            "transcriptPath": str(tmp_path / "transcript.jsonl"), "modelName": "fixture-model",
            "lastUserInput": "Explain how governed citations work"}


def test_session_start_uses_shared_core_and_emits_only_inject_steps(payload, tmp_path, monkeypatch, capsys):
    from memorymaster.recall import remote

    calls = []

    def recall(query, data, *, db_path):
        calls.append((query, data, db_path))
        assert hooks._internal()
        print("dependency diagnostic must not reach agy")
        return "\n- [mm-7] governed fact", "shared"

    monkeypatch.setattr(remote, "hook_recall", recall)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hooks.main("SessionStart", db_path="fixture-only.db") == 0
    out = capsys.readouterr()
    assert out.err == ""
    assert json.loads(out.out) == {"injectSteps": [{"ephemeralMessage": "[MemoryMaster recall]\n\n- [mm-7] governed fact"}]}
    query, data, db = calls[0]
    assert query == payload["lastUserInput"]
    assert data == {"cwd": payload["workspacePaths"][0], "session_id": "fixture-session",
                    "prompt": query, "model": "fixture-model", "transcript_path": payload["transcriptPath"]}
    assert db == "fixture-only.db"
    receipt = json.loads((tmp_path / "events.jsonl").read_text())
    assert {key: receipt[key] for key in ("cli", "event", "count")} == {"cli": "agy", "event": "SessionStart", "count": 1}
    assert not hooks._internal()


def test_first_session_without_prompt_recalls_workspace_context(payload, monkeypatch):
    seen = []
    payload["lastUserInput"] = ""
    monkeypatch.setattr("memorymaster.recall.remote.hook_recall",
                        lambda query, data, **kw: (seen.append(query) or "project fact", "local"))
    assert "project fact" in hooks.run_hook("SessionStart", payload, db_path="unused")
    assert seen == ["Project context for project:fixture"]


@pytest.mark.parametrize("event", ["PreInvocation", "PostInvocation", "UserPromptSubmit", "SessionEnd"])
def test_no_per_prompt_injection(event, payload, monkeypatch):
    monkeypatch.setattr("memorymaster.recall.remote.hook_recall", lambda *a, **kw: pytest.fail("unexpected recall"))
    assert hooks.run_hook(event, payload, db_path="unused") == ""


@pytest.mark.parametrize("marker,value", [
    (hooks.INTERNAL_ENV, "1"), ("AGY_BATCH_PROFILE", "agy-challenger"),
    ("AGY_BATCH_PROFILE", "memorymaster"), ("HOME", "/fixture/.gemini-batch/memorymaster"),
    ("USERPROFILE", "C:\\fixture\\.gemini-batch\\agy-challenger"),
])
@pytest.mark.parametrize("event", ["SessionStart", "Stop"])
def test_internal_jobs_do_not_read_or_capture(marker, value, event, payload, tmp_path, monkeypatch):
    monkeypatch.setenv(marker, value)
    monkeypatch.setattr("memorymaster.recall.remote.hook_recall", lambda *a, **kw: pytest.fail("recursive recall"))
    monkeypatch.setattr("memorymaster.dreaming.capture.capture_hook_payload", lambda *a, **kw: pytest.fail("recursive capture"))
    assert hooks.run_hook(event, payload, db_path="unused") == ""
    assert not (tmp_path / "capture.db").exists()
    assert not (tmp_path / "events.jsonl").exists()


@pytest.mark.parametrize("change", [
    {"workspacePaths": []}, {"workspacePaths": ["relative"]}, {"workspacePaths": "bad"},
    {"workspacePaths": ["/one", "/two"]}, {"conversationId": None}, {"lastUserInput": []},
])
def test_invalid_identity_never_falls_back_to_config_cwd(change, payload, monkeypatch):
    payload.update(change)
    monkeypatch.setattr("memorymaster.recall.remote.hook_recall", lambda *a, **kw: pytest.fail("unsafe recall"))
    assert hooks.run_hook("SessionStart", payload, db_path="unused") == ""


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_recall_has_no_output_or_receipt(text, payload, tmp_path, monkeypatch):
    monkeypatch.setattr("memorymaster.recall.remote.hook_recall", lambda *a, **kw: (text, "shared"))
    assert hooks.run_hook("SessionStart", payload, db_path="unused") == ""
    assert not (tmp_path / "events.jsonl").exists()


def test_recall_failure_is_silent_and_restores_guard(payload, tmp_path, monkeypatch, capsys):
    def fail(*a, **kw):
        print("diagnostic")
        raise RuntimeError("private failure")

    monkeypatch.setattr("memorymaster.recall.remote.hook_recall", fail)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hooks.main("SessionStart", db_path="unused") == 0
    assert capsys.readouterr() == ("", "")
    assert not hooks._internal()
    assert not (tmp_path / "events.jsonl").exists()


@pytest.mark.parametrize("raw", ["[", "null", "[]", '"string"'])
def test_bad_stdin_exits_zero_without_output(raw, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    assert hooks.main("SessionStart", db_path="unused") == 0
    assert capsys.readouterr() == ("", "")


def test_automated_notification_is_silent(payload, monkeypatch):
    payload["lastUserInput"] = "<task-notification>internal completion"
    monkeypatch.setattr("memorymaster.recall.remote.hook_recall", lambda *a, **kw: pytest.fail("unexpected recall"))
    assert hooks.run_hook("SessionStart", payload, db_path="unused") == ""


def test_stop_queues_fixture_once_and_records_capture_receipt(payload, tmp_path, capsys):
    from memorymaster.dreaming.ledger import DreamLedger

    transcript = Path(payload["transcriptPath"])
    transcript.write_text("\n".join(json.dumps(row) for row in [
        {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "SQLite owns the lifecycle."},
        {"step_index": 1, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "Confirmed with a cited fixture."},
    ]) + "\n", encoding="utf-8")
    for _ in range(2):
        assert hooks.run_hook("Stop", payload, db_path="unused") == ""
    assert capsys.readouterr() == ("", "")
    assert DreamLedger(tmp_path / "capture.db").status()["queue"]["captured"] == 1
    receipts = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert len(receipts) == 1
    assert receipts[0]["event"] == "Stop"
    assert receipts[0]["cli"] == "agy"
    assert receipts[0]["count"] == 1


@pytest.mark.parametrize("mode", ["disabled", "missing", "error"])
def test_stop_failure_or_disabled_capture_is_silent(mode, payload, tmp_path, monkeypatch):
    if mode == "disabled":
        monkeypatch.setenv("MEMORYMASTER_DREAM_ENABLED", "0")
    elif mode == "missing":
        payload["transcriptPath"] = None
    else:
        def fail(*a, **kw):
            raise OSError("fixture failure")
        monkeypatch.setattr("memorymaster.dreaming.capture.capture_hook_payload", fail)
    assert hooks.run_hook("Stop", payload, db_path="unused") == ""
    assert not (tmp_path / "events.jsonl").exists()
