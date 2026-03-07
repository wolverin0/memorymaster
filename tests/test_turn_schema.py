from __future__ import annotations

from memorymaster.turn_schema import normalize_turn_row


def test_normalize_turn_row_explicit_shape() -> None:
    row = {
        "session_id": "s1",
        "thread_id": "t1",
        "turn_id": "turn-1",
        "user_text": "hi",
        "assistant_text": "hello",
        "observations": ["tool started", "tool done"],
        "timestamp": "2026-03-03T12:00:00+00:00",
    }

    got = normalize_turn_row(row)
    assert got.session_id == "s1"
    assert got.thread_id == "t1"
    assert got.turn_id == "turn-1"
    assert got.user_text == "hi"
    assert got.assistant_text == "hello"
    assert got.observations == ["tool started", "tool done"]
    assert got.timestamp == "2026-03-03T12:00:00+00:00"


def test_normalize_turn_row_events_shape() -> None:
    row = {
        "turn_id": "turn-evt-1",
        "session_id": "s-evt",
        "events": [
            {"role": "user", "text": "first user"},
            {"role": "tool", "text": "tool output"},
            {"role": "assistant", "text": "first assistant"},
            {"role": "user", "text": "second user"},
            {"role": "assistant", "text": "second assistant"},
            {"role": "system", "text": "side note"},
        ],
    }

    got = normalize_turn_row(row)
    assert got.session_id == "s-evt"
    assert got.thread_id == ""
    assert got.turn_id == "turn-evt-1"
    assert got.user_text == "first user\nsecond user"
    assert got.assistant_text == "first assistant\nsecond assistant"
    assert got.observations == ["tool output", "side note"]
    assert got.timestamp == ""


def test_normalize_turn_row_messages_block_list_shape() -> None:
    row = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "image", "url": "https://example.com/image.png"},
                    {"type": "text", "text": "world"},
                ],
            },
            {"role": "assistant", "content": "response"},
            {
                "role": "tool",
                "content": [
                    {"type": "text", "text": "tool line 1"},
                    {"type": "text", "text": "tool line 2"},
                ],
            },
        ]
    }

    got = normalize_turn_row(row)
    assert got.session_id == ""
    assert got.thread_id == ""
    assert got.user_text == "hello\nworld"
    assert got.assistant_text == "response"
    assert got.observations == ["tool line 1\ntool line 2"]
    assert got.timestamp == ""


def test_normalize_turn_row_generates_fallback_turn_id_when_missing(monkeypatch) -> None:
    monkeypatch.setattr("memorymaster.turn_schema.time.time", lambda: 1700000000.123)

    got = normalize_turn_row({})
    assert got.turn_id == "turn-1700000000123"
    assert got.session_id == ""
    assert got.thread_id == ""
    assert got.user_text == ""
    assert got.assistant_text == ""
    assert got.observations == []
    assert got.timestamp == ""
