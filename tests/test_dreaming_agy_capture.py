from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memorymaster.dreaming.capture import capture_hook_payload, capture_transcript
from memorymaster.dreaming.ledger import DreamLedger


_ELIGIBILITY_NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_SCOPE_ALIASES", str(tmp_path / "absent-aliases.json"))
    workspace = tmp_path / "fixture-project"
    (workspace / ".git").mkdir(parents=True)
    transcript = tmp_path / "brain" / "fixture-session" / ".system_generated" / "logs" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    ledger = DreamLedger(tmp_path / "dream.db")
    payload = {
        "conversationId": "fixture-session",
        "workspacePaths": [str(workspace)],
        "transcriptPath": str(transcript),
        "modelName": "fixture-model",
        "lastUserInput": "This payload text is not transcript evidence.",
    }
    return transcript, ledger, payload


def _row(step, text, *, role="user", **extra):
    return {
        "step_index": step,
        "type": "USER_INPUT" if role == "user" else "PLANNER_RESPONSE",
        "source": "USER_EXPLICIT" if role == "user" else "MODEL",
        "content": text,
        "created_at": "2026-10-26T12:00:00Z",
        **extra,
    }


def _write(path: Path, *rows, append=False):
    with path.open("a" if append else "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _capture(session):
    _, ledger, payload = session
    return capture_hook_payload(payload, provider="antigravity", ledger=ledger)


def test_camel_case_payload_captures_only_transcript_evidence(session):
    transcript, ledger, payload = session
    _write(transcript, _row(0, "Keep SQLite authoritative."), _row(1, "The vector index only proposes IDs.", role="model"))

    result = _capture(session)

    assert result["queued"] == 1
    capture = ledger.get_capture(result["capture_id"])
    assert capture["provider"] == "antigravity"
    assert capture["scope"] == "project:fixture-project"
    assert capture["session_hash"] == hashlib.sha256(payload["conversationId"].encode()).hexdigest()
    assert [(m["role"], m["text"]) for m in capture["messages"]] == [
        ("user", "Keep SQLite authoritative."),
        ("assistant", "The vector index only proposes IDs."),
    ]
    assert capture["cursor_start"] == 0
    assert capture["cursor_end"] == transcript.stat().st_size
    assert payload["lastUserInput"] not in json.dumps(capture)


def test_repeat_and_duplicate_revision_queue_nothing(session):
    transcript, ledger, _ = session
    row = _row(0, "Keep retries bounded.")
    _write(transcript, row, _row(1, "I will keep retries bounded.", role="model"))
    assert _capture(session)["queued"] == 1
    assert _capture(session) == {"queued": 0, "reason": "no_increment"}
    _write(transcript, row, append=True)
    assert _capture(session) == {"queued": 0, "reason": "no_increment"}
    assert ledger.status()["queue"]["captured"] == 1


def test_appended_and_revised_steps_preserve_latest_source_identity(session):
    transcript, ledger, _ = session
    _write(transcript, _row(0, "Original requirement."), _row(1, "Original answer.", role="model"))
    original = ledger.get_capture(_capture(session)["capture_id"])
    _write(transcript, _row(0, "Corrected requirement."), _row(2, "New answer.", role="model"), append=True)

    result = _capture(session)

    assert result["messages"] == 2
    revised = ledger.get_capture(result["capture_id"])
    assert [m["text"] for m in revised["messages"]] == ["Corrected requirement.", "New answer."]
    assert revised["messages"][0]["message_id"] == original["messages"][0]["message_id"]
    assert revised["cursor_start"] == original["cursor_end"]
    assert revised["cursor_end"] == transcript.stat().st_size
    assert _capture(session)["reason"] == "no_increment"
    assert ledger.status()["queue"]["captured"] == 2


def test_capture_uses_latest_revision_before_first_capture(session):
    transcript, ledger, _ = session
    _write(transcript, _row(0, "Stale text."), _row(0, "Current text."), _row(1, "Current answer.", role="model"))
    capture = ledger.get_capture(_capture(session)["capture_id"])
    assert [m["text"] for m in capture["messages"]] == ["Current text.", "Current answer."]


def test_truncated_field_requires_full_replacement_without_advancing(session):
    transcript, ledger, _ = session
    _write(transcript, _row(0, "Partial...", truncated_fields=["content"]), _row(1, "A complete response.", role="model"))
    assert _capture(session) == {"queued": 0, "reason": "invalid_transcript"}
    assert ledger.status()["queue"].get("captured", 0) == 0
    full = transcript.with_name("transcript_full.jsonl")
    _write(full, _row(0, "The entire original field."))
    capture = ledger.get_capture(_capture(session)["capture_id"])
    assert capture["messages"][0]["text"] == "The entire original field."
    _write(full, _row(0, "The revised full field."), append=True)
    revised = ledger.get_capture(_capture(session)["capture_id"])
    assert [m["text"] for m in revised["messages"]] == ["A complete response.", "The revised full field."]
    assert revised["turn_count"] == 2
    assert revised["id"] in {row["id"] for row in ledger.eligible(idle_minutes=0, max_sessions=10, now=_ELIGIBILITY_NOW)}
    assert _capture(session)["reason"] == "no_increment"


@pytest.mark.parametrize("workspace", [None, [], "unexpected-string", [None], [""], ["relative"], ["/one", "/two"]])
def test_missing_or_ambiguous_workspace_fails_closed(session, workspace):
    transcript, ledger, payload = session
    _write(transcript, _row(0, "Do not leak scope."))
    payload["workspacePaths"] = workspace
    assert _capture(session) == {"queued": 0, "reason": "unresolved_scope"}
    assert ledger.status()["queue"].get("captured", 0) == 0


@pytest.mark.parametrize("contents", ["", "not json\n", '{"step_index": -1}\n', '[]\n'])
def test_empty_or_malformed_transcript_queues_nothing(session, contents):
    transcript, ledger, _ = session
    transcript.write_text(contents, encoding="utf-8")
    assert _capture(session)["queued"] == 0
    assert ledger.status()["queue"].get("captured", 0) == 0


def test_missing_transcript_and_partial_record_queue_nothing(session):
    transcript, ledger, _ = session
    assert _capture(session)["queued"] == 0
    _write(transcript, _row(0, "A complete first turn."))
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_row(1, "Unfinished response.", role="model")))
    assert _capture(session)["queued"] == 0
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert _capture(session)["queued"] == 1
    assert ledger.status()["queue"]["captured"] == 1


@pytest.mark.parametrize("field, reason", [("transcriptPath", "no_transcript"), ("conversationId", "no_session")])
def test_missing_native_identity_queues_nothing(session, field, reason):
    transcript, ledger, payload = session
    _write(transcript, _row(0, "Only native transcript evidence is eligible."))
    payload.pop(field)
    assert _capture(session) == {"queued": 0, "reason": reason}
    assert ledger.status()["queue"].get("captured", 0) == 0


def test_sanitizes_and_filters_memory_tool_contamination_across_captures(session):
    transcript, ledger, _ = session
    _write(transcript, _row(0, "Use sk-LiveSecret1234567890abcd from C:\\Users\\alice\\private.txt"),
           _row(1, "Keep credentials outside memory.", role="model"))
    first = ledger.get_capture(_capture(session)["capture_id"])
    assert "sk-LiveSecret" not in json.dumps(first)
    assert "alice" not in json.dumps(first)
    _write(transcript, {"step_index": 2, "type": "TOOL_CALL", "tool_calls": [{"name": "mcp__memorymaster__recall"}]},
           _row(3, "Recalled memory must not become evidence.", role="model"), append=True)
    assert _capture(session)["queued"] == 0
    _write(transcript, _row(4, "A fresh authored requirement."), append=True)
    capture = ledger.get_capture(_capture(session)["capture_id"])
    assert [m["text"] for m in capture["messages"]] == ["Keep credentials outside memory.", "A fresh authored requirement."]
    assert capture["messages"][-1]["text"] == "A fresh authored requirement."
    assert "Recalled memory must not become evidence." not in json.dumps(capture)
    assert "sk-LiveSecret" not in json.dumps(capture)


def test_direct_capture_requires_workspace_and_respects_live_mode(session):
    transcript, ledger, payload = session
    _write(transcript, _row(0, "Non Git workspace is explicit hook evidence."), _row(1, "Keep the hook scope.", role="model"))
    workspace = Path(payload["workspacePaths"][0]) / "non-git"
    workspace.mkdir()
    args = dict(provider="antigravity", session_id="fixture-session", ledger=ledger)
    assert capture_transcript(transcript, cwd=None, **args)["reason"] == "unresolved_scope"
    result = capture_transcript(transcript, cwd=str(workspace), live_workspace=True, **args)
    assert result["queued"] == 1
    assert ledger.get_capture(result["capture_id"])["scope"] == "project:fixture-project"


def test_two_single_message_stops_become_worker_eligible(session):
    transcript, ledger, _ = session
    _write(transcript, _row(0, "Keep the original user turn."))
    assert _capture(session) == {"queued": 0, "reason": "insufficient_turns"}
    assert ledger.eligible(idle_minutes=0, max_sessions=10, now=_ELIGIBILITY_NOW) == []
    assert ledger.status()["queue"].get("captured", 0) == 0
    session_hash = hashlib.sha256(b"fixture-session").hexdigest()
    assert ledger.scan_state(f"agy-revisions:{session_hash}:project:fixture-project") is None

    _write(transcript, _row(1, "The response completes the session context.", role="model"), append=True)
    result = _capture(session)
    eligible = ledger.eligible(idle_minutes=0, max_sessions=10, now=_ELIGIBILITY_NOW)
    assert [capture["id"] for capture in eligible] == [result["capture_id"]]
    assert [message["text"] for message in eligible[0]["messages"]] == [
        "Keep the original user turn.", "The response completes the session context.",
    ]
    assert _capture(session)["reason"] == "no_increment"


def test_isolated_step_correction_keeps_context_and_is_worker_eligible(session):
    transcript, ledger, _ = session
    _write(transcript, _row(0, "Original requirement."), _row(1, "Preserved context.", role="model"))
    first = _capture(session)
    _write(transcript, _row(0, "Corrected requirement."), append=True)
    revised = _capture(session)
    eligible = {capture["id"]: capture for capture in ledger.eligible(idle_minutes=0, max_sessions=10, now=_ELIGIBILITY_NOW)}
    assert set(eligible) == {first["capture_id"], revised["capture_id"]}
    assert [message["text"] for message in eligible[revised["capture_id"]]["messages"]] == [
        "Preserved context.", "Corrected requirement.",
    ]
    assert eligible[revised["capture_id"]]["turn_count"] == 2
    assert _capture(session)["reason"] == "no_increment"


def test_old_step_revision_follows_nearest_context_and_survives_tail_selection(session):
    transcript, ledger, _ = session
    _write(transcript, _row(0, "Original requirement."), *(
        _row(step, f"Unchanged response {step}.", role="model") for step in range(1, 12)
    ))
    original = ledger.get_capture(_capture(session)["capture_id"])
    _write(transcript, _row(0, "Corrected oldest requirement."), append=True)

    revised = ledger.get_capture(_capture(session)["capture_id"])

    assert [m["text"] for m in revised["messages"]] == ["Unchanged response 1.", "Corrected oldest requirement."]
    assert revised["messages"][-1]["message_id"] == original["messages"][0]["message_id"]
    assert revised["turn_count"] == 2
    assert _capture(session)["reason"] == "no_increment"
