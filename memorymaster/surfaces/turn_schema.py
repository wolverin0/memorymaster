from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class NormalizedTurn:
    session_id: str
    thread_id: str
    turn_id: str
    user_text: str
    assistant_text: str
    observations: list[str]
    timestamp: str


def _fallback_turn_id() -> str:
    return f"turn-{int(time.time() * 1000)}"


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _normalize_observations(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [_to_str(item).strip() for item in value if _to_str(item).strip()]
    text = _to_str(value).strip()
    return [text] if text else []


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text = _to_str(block.get("text"))
            if text:
                parts.append(text)
        return "\n".join(parts)
    return _to_str(content)


def _events_to_texts(events: Any) -> tuple[str, str, list[str]]:
    user_parts: list[str] = []
    assistant_parts: list[str] = []
    observations: list[str] = []
    if not isinstance(events, list):
        return "", "", []

    for event in events:
        if not isinstance(event, dict):
            continue
        role = _to_str(event.get("role")).strip().lower()
        text = _to_str(event.get("text")).strip()
        if not text:
            continue
        if role == "user":
            user_parts.append(text)
        elif role == "assistant":
            assistant_parts.append(text)
        else:
            observations.append(text)
    return "\n".join(user_parts), "\n".join(assistant_parts), observations


def _messages_to_texts(messages: Any) -> tuple[str, str, list[str]]:
    user_parts: list[str] = []
    assistant_parts: list[str] = []
    observations: list[str] = []
    if not isinstance(messages, list):
        return "", "", []

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = _to_str(message.get("role")).strip().lower()
        text = _content_to_text(message.get("content")).strip()
        if not text:
            continue
        if role == "user":
            user_parts.append(text)
        elif role == "assistant":
            assistant_parts.append(text)
        else:
            observations.append(text)
    return "\n".join(user_parts), "\n".join(assistant_parts), observations


def normalize_turn_row(row: dict[str, Any]) -> NormalizedTurn:
    session_id = _to_str(row.get("session_id")).strip()
    thread_id = _to_str(row.get("thread_id")).strip()
    timestamp = _to_str(row.get("timestamp")).strip()
    turn_id = _to_str(row.get("turn_id")).strip() or _fallback_turn_id()

    user_text = ""
    assistant_text = ""
    observations: list[str] = []

    if isinstance(row.get("events"), list):
        user_text, assistant_text, observations = _events_to_texts(row.get("events"))
    elif isinstance(row.get("messages"), list):
        user_text, assistant_text, observations = _messages_to_texts(row.get("messages"))
    else:
        user_text = _to_str(row.get("user_text"))
        assistant_text = _to_str(row.get("assistant_text"))
        observations = _normalize_observations(row.get("observations"))

    return NormalizedTurn(
        session_id=session_id,
        thread_id=thread_id,
        turn_id=turn_id,
        user_text=user_text,
        assistant_text=assistant_text,
        observations=observations,
        timestamp=timestamp,
    )
