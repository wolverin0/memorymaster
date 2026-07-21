"""Fast transcript adapters used by Codex and Claude lifecycle hooks."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memorymaster.core.capture_control import CaptureLedger
from memorymaster.core.security import redact_text
from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.models import CaptureEnvelope, DreamMessage


_ABSOLUTE_WINDOWS_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:\\|[A-Z]:/)[^\r\n\t\"'<>|]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text_parts(content: Any, allowed: set[str]) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [str(part.get("text", "")) for part in content if isinstance(part, dict) and str(part.get("type", "text")) in allowed]
    return "\n".join(part for part in parts if part.strip())


def _conversation_record(entry: dict[str, Any], provider: str) -> tuple[str, str] | None:
    if provider == "codex":
        payload = entry.get("payload")
        if entry.get("type") != "response_item" or not isinstance(payload, dict) or payload.get("type") != "message":
            return None
        role = str(payload.get("role", ""))
        text = _text_parts(payload.get("content"), {"input_text", "output_text", "text"})
    else:
        message = entry.get("message")
        if not isinstance(message, dict):
            message = entry if "role" in entry else None
        if not isinstance(message, dict) or str(entry.get("type", message.get("role", ""))) not in {"user", "assistant"}:
            return None
        role = str(message.get("role", entry.get("type", "")))
        text = _text_parts(message.get("content", ""), {"text", "input_text", "output_text"})
    normalized_role = "assistant" if role in {"assistant", "model"} else "user" if role == "user" else ""
    return (normalized_role, text) if normalized_role and text.strip() else None


def _sanitize(text: str) -> str:
    redacted, _ = redact_text(text)
    redacted = _ABSOLUTE_WINDOWS_PATH.sub("[REDACTED:absolute_path]", redacted)
    return redacted.strip()[:16_000]


def parse_transcript_lines(text: str, provider: str) -> list[DreamMessage]:
    messages: list[DreamMessage] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in text.splitlines():
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        record = _conversation_record(entry, provider.lower())
        if record is None:
            continue
        role, content = record
        sanitized = _sanitize(content)
        if not sanitized:
            continue
        timestamp = str(entry.get("timestamp") or _now())
        identity = (role, sanitized, timestamp)
        if identity in seen:
            continue
        seen.add(identity)
        raw_id = str(entry.get("uuid") or entry.get("id") or "|".join(identity))
        message_id = "dm-" + hashlib.sha256(raw_id.encode("utf-8", errors="replace")).hexdigest()[:20]
        messages.append(DreamMessage(message_id, role, sanitized, timestamp))
    return messages


def _scope(cwd: str | None) -> str:
    if not cwd:
        return "global"
    slug = re.sub(r"[^a-z0-9]+", "-", Path(cwd).name.lower()).strip("-")
    return f"project:{slug or 'unknown'}"


def capture_transcript(
    transcript_path: str | Path, *, provider: str, session_id: str, cwd: str | None, ledger: DreamLedger,
) -> dict[str, Any]:
    session_hash = hashlib.sha256((session_id or "unknown").encode("utf-8", errors="replace")).hexdigest()
    cursor_ledger = CaptureLedger(ledger.db_path)
    chunk = cursor_ledger.read_increment(transcript_path, f"dream:{provider}:{session_hash}")
    if not chunk.text:
        return {"queued": 0, "reason": "no_increment"}
    messages = parse_transcript_lines(chunk.text, provider)
    if not messages:
        cursor_ledger.commit_cursor(chunk)
        return {"queued": 0, "reason": "no_conversation"}
    content_hash = hashlib.sha256(json.dumps([m.to_dict() for m in messages], sort_keys=True).encode("utf-8")).hexdigest()
    envelope = CaptureEnvelope(
        provider=provider.lower(), session_hash=session_hash, scope=_scope(cwd), captured_at=_now(),
        last_activity_at=messages[-1].timestamp or _now(), messages=tuple(messages),
        cursor_start=chunk.start_offset, cursor_end=chunk.end_offset, content_hash=content_hash,
    )
    capture_id = ledger.enqueue(envelope)
    cursor_ledger.commit_cursor(chunk)
    return {"queued": 1, "capture_id": capture_id, "messages": len(messages)}


def capture_hook_payload(payload: dict[str, Any], *, provider: str, ledger: DreamLedger) -> dict[str, Any]:
    transcript_path = str(payload.get("transcript_path") or "")
    if not transcript_path:
        return {"queued": 0, "reason": "no_transcript"}
    return capture_transcript(
        transcript_path,
        provider=provider,
        session_id=str(payload.get("session_id") or payload.get("thread_id") or "unknown"),
        cwd=str(payload.get("cwd") or "") or None,
        ledger=ledger,
    )
