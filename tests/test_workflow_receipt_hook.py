from __future__ import annotations

import json
from pathlib import Path

import pytest

from memorymaster.surfaces import setup_hooks
from memorymaster.workflow_intelligence.hook import evaluate_payload, main
from memorymaster.workflow_intelligence.storage import WorkflowStore


def test_hook_is_off_by_default_and_does_not_create_database(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "workflow.db"
    monkeypatch.delenv("MEMORYMASTER_WORKFLOW_RECEIPTS", raising=False)
    monkeypatch.setenv("MEMORYMASTER_WORKFLOW_DB", str(db))
    result = evaluate_payload({"session_id": "s", "tool_calls": []})
    assert result == {"mode": "off", "warnings": []}
    assert not db.exists()


def test_shadow_receipt_is_content_free_and_read_only_safe(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "workflow.db"
    monkeypatch.setenv("MEMORYMASTER_WORKFLOW_RECEIPTS", "shadow")
    monkeypatch.setenv("MEMORYMASTER_WORKFLOW_DB", str(db))
    monkeypatch.setenv("MEMORYMASTER_WORKFLOW_TRANSCRIPT_ROOTS", str(tmp_path))
    payload = {
        "session_id": "raw-session-id",
        "provider": "codex",
        "assistant_text": "Done and working. secret-token-should-not-persist",
        "actions": [
            {"kind": "read", "name": "read_file", "status": "success"},
        ],
    }
    result = evaluate_payload(payload)
    assert result["warnings"] == []

    store = WorkflowStore(db)
    try:
        row = store.rows("completion_receipts")[0]
        serialized = json.dumps(dict(row))
        assert "raw-session-id" not in serialized
        assert "secret-token" not in serialized
        assert row["mutation_seen"] == 0
    finally:
        store.close()


def test_shadow_warns_on_mutation_claim_without_later_verification(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "workflow.db"
    monkeypatch.setenv("MEMORYMASTER_WORKFLOW_RECEIPTS", "shadow")
    monkeypatch.setenv("MEMORYMASTER_WORKFLOW_DB", str(db))
    result = evaluate_payload(
        {
            "session_id": "session-a",
            "provider": "claude",
            "assistant_text": "The fix is complete.",
            "actions": [{"kind": "mutation", "name": "Edit", "status": "success"}],
        }
    )
    assert result["warnings"] == ["completion_without_verification"]
    assert result["emit_advisory"] is False


def test_shadow_derives_current_turn_from_real_claude_transcript(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "workflow.db"
    transcript = tmp_path / "session.jsonl"
    rows = [
        {"message": {"role": "user", "content": "Fix it"}},
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "edit-1", "name": "Edit", "input": {"file_path": "app.py"}}
        ]}},
        {"message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "edit-1", "content": "ok"}
        ]}},
        {"message": {"role": "assistant", "content": [{"type": "text", "text": "The fix is done."}]}},
    ]
    transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    monkeypatch.setenv("MEMORYMASTER_WORKFLOW_RECEIPTS", "shadow")
    monkeypatch.setenv("MEMORYMASTER_WORKFLOW_DB", str(db))
    monkeypatch.setenv("MEMORYMASTER_WORKFLOW_TRANSCRIPT_ROOTS", str(tmp_path))

    result = evaluate_payload({
        "session_id": "s-real", "provider": "claude", "transcript_path": str(transcript),
    })

    assert result["warnings"] == ["completion_without_verification"]
    store = WorkflowStore(db)
    try:
        row = store.rows("completion_receipts")[0]
        assert row["mutation_seen"] == 1
        assert row["verification_tier"] == "none"
        assert str(transcript) not in json.dumps(dict(row))
    finally:
        store.close()


def test_hook_refuses_transcript_outside_configured_roots(tmp_path: Path, monkeypatch) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside" / "session.jsonl"
    outside.parent.mkdir()
    outside.write_text(json.dumps({"message": {"role": "assistant", "content": "Done"}}) + "\n")
    monkeypatch.setenv("MEMORYMASTER_WORKFLOW_RECEIPTS", "shadow")
    monkeypatch.setenv("MEMORYMASTER_WORKFLOW_DB", str(tmp_path / "workflow.db"))
    monkeypatch.setenv("MEMORYMASTER_WORKFLOW_TRANSCRIPT_ROOTS", str(allowed))

    result = evaluate_payload({
        "session_id": "outside", "provider": "claude", "transcript_path": str(outside),
    })

    assert result["warnings"] == []
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        assert store.rows("completion_receipts")[0]["mutation_seen"] == 0
    finally:
        store.close()


def test_hook_main_never_blocks_on_invalid_input(monkeypatch, capsys) -> None:
    monkeypatch.setenv("MEMORYMASTER_WORKFLOW_RECEIPTS", "advisory")
    monkeypatch.setattr("sys.stdin.read", lambda: "not-json")
    assert main([]) == 0
    assert capsys.readouterr().out == ""


def test_setup_workflow_receipts_is_explicit_and_shadow_only(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    claude = home / ".claude"
    codex = home / ".codex"
    monkeypatch.setattr(setup_hooks, "HOME", home)
    monkeypatch.setattr(setup_hooks, "CLAUDE_DIR", claude)
    monkeypatch.setattr(setup_hooks, "CODEX_DIR", codex)
    monkeypatch.setattr(setup_hooks, "PROJECT_ROOT", tmp_path / "project")

    result = setup_hooks.configure_workflow_receipts(
        mode="shadow", install_claude=True, install_codex=True,
        workflow_db=tmp_path / "workflow.db",
    )

    assert result["mode"] == "shadow"
    claude_settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
    codex_settings = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))
    assert claude_settings["env"]["MEMORYMASTER_WORKFLOW_RECEIPTS"] == "shadow"
    assert codex_settings["env"]["MEMORYMASTER_WORKFLOW_RECEIPTS"] == "shadow"
    assert "memorymaster-workflow-receipt" in json.dumps(claude_settings["hooks"]["Stop"])


def test_setup_advisory_fails_closed_before_shadow_gate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="14-day shadow gate"):
        setup_hooks.configure_workflow_receipts(
            mode="advisory", install_claude=False, install_codex=False,
            workflow_db=tmp_path / "workflow.db",
        )
