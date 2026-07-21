from __future__ import annotations

import json
from pathlib import Path

from memorymaster.dreaming.capture import capture_transcript, parse_transcript_lines
from memorymaster.dreaming.ledger import DreamLedger


def test_codex_parser_keeps_conversation_and_drops_reasoning_and_tools() -> None:
    rows = [
        {"type": "response_item", "timestamp": "2026-07-21T10:00:00Z", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Remember the blue layout."}] }},
        {"type": "response_item", "payload": {"type": "reasoning", "summary": "private chain"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call_output", "output": "secret tool output"}},
        {"type": "response_item", "timestamp": "2026-07-21T10:01:00Z", "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "The blue layout is now the default."}] }},
    ]

    messages = parse_transcript_lines("\n".join(json.dumps(row) for row in rows), "codex")

    assert [(message.role, message.text) for message in messages] == [
        ("user", "Remember the blue layout."),
        ("assistant", "The blue layout is now the default."),
    ]
    assert "private chain" not in repr(messages)
    assert "secret tool output" not in repr(messages)


def test_claude_parser_redacts_before_auxiliary_persistence(tmp_path: Path) -> None:
    secret = "sk-LiveSecret1234567890abcd"
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text(
        json.dumps({
            "type": "user",
            "timestamp": "2026-07-21T10:00:00Z",
            "message": {"role": "user", "content": f"Use {secret} from C:\\Users\\alice\\private.txt"},
        }) + "\n" + json.dumps({
            "type": "assistant",
            "timestamp": "2026-07-21T10:01:00Z",
            "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "do not persist"},
                {"type": "text", "text": "I will keep credentials out of memory."},
            ]},
        }) + "\n",
        encoding="utf-8",
    )
    ledger = DreamLedger(tmp_path / "capture.db")

    result = capture_transcript(
        transcript,
        provider="claude",
        session_id="sensitive-session",
        cwd=str(tmp_path / "my project"),
        ledger=ledger,
    )

    assert result["queued"] == 1
    persisted = json.dumps(ledger.get_capture(result["capture_id"]), ensure_ascii=False)
    assert secret not in persisted
    assert "C:\\Users\\alice" not in persisted
    assert "do not persist" not in persisted
    assert "[REDACTED:" in persisted


def test_capture_cursor_is_replay_safe(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({
        "message": {"role": "user", "content": "A durable preference worth capturing."}
    }) + "\n", encoding="utf-8")
    ledger = DreamLedger(tmp_path / "capture.db")

    first = capture_transcript(
        transcript, provider="claude", session_id="same", cwd=str(tmp_path), ledger=ledger
    )
    second = capture_transcript(
        transcript, provider="claude", session_id="same", cwd=str(tmp_path), ledger=ledger
    )

    assert first["queued"] == 1
    assert second == {"queued": 0, "reason": "no_increment"}
    assert ledger.status()["queue"]["captured"] == 1
