"""Fast transcript adapters used by Codex and Claude lifecycle hooks."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from memorymaster.core.capture_control import CaptureLedger
from memorymaster.core.security import redact_text
from memorymaster.core.scope_utils import scope_from_cwd
from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.models import CaptureEnvelope, DreamMessage
from memorymaster.dreaming.self_observation import is_memory_tool, strip_self_observation


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


def _conversation_record(
    entry: dict[str, Any], provider: str, *, state: dict[str, Any] | None = None,
) -> tuple[str, str] | None:
    state = state if state is not None else {}
    payload = entry.get("payload") if provider == "codex" else entry.get("message", entry)
    payload = payload if isinstance(payload, dict) else entry
    content = payload.get("content", [])
    blocks = content if isinstance(content, list) else []
    calls = [payload, *[block for block in blocks if isinstance(block, dict)]]
    has_tool_result = False
    for call in calls:
        has_tool_result |= call.get("type") in {"tool_result", "function_call_output", "custom_tool_call_output"}
        name = str(call.get("name") or call.get("tool_name") or "")
        call_id = str(call.get("call_id") or call.get("tool_use_id") or call.get("id") or "")
        if is_memory_tool(name) or (call_id and call_id in state.get("memory_tool_ids", ())):
            strip_self_observation("", role="tool", tool_name=name, metadata={"state": state, "tool_call_id": call_id})
            # Text alongside a memory call/result shares the structural origin.
            return None
    if has_tool_result:
        return None
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
    clean = strip_self_observation(text, role=normalized_role, metadata={
        "state": state,
        "injected": bool(entry.get("hook_additional_context") or entry.get("isMeta") or payload.get("hook_additional_context")),
        "generated_session": bool(entry.get("backfill_generated") or entry.get("memorymaster_generated")),
    })
    return (normalized_role, clean.text) if normalized_role and clean.text else None


def _sanitize(text: str) -> str:
    redacted, _ = redact_text(text)
    redacted = _ABSOLUTE_WINDOWS_PATH.sub("[REDACTED:absolute_path]", redacted)
    return redacted.strip()[:16_000]


def parse_transcript_lines(
    text: str, provider: str, *, state: dict[str, Any] | None = None,
) -> list[DreamMessage]:
    state = state if state is not None else {}
    messages: list[DreamMessage] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in text.splitlines():
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        record = _conversation_record(entry, provider.lower(), state=state)
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
    return scope_from_cwd(cwd)


def capture_transcript(
    transcript_path: str | Path, *, provider: str, session_id: str, cwd: str | None, ledger: DreamLedger,
    record_source_mtime: bool = False,
) -> dict[str, Any]:
    """Queue the new bytes of a transcript.

    ``record_source_mtime`` is the beat scan's: it sets the capture's source mtime. Lifecycle hooks
    leave it off, so they never create beat data (an inactive beat stays invisible to the review);
    they only refresh a source mtime the beat already set when they coalesce into that capture.
    """
    session_hash = hashlib.sha256((session_id or "unknown").encode("utf-8", errors="replace")).hexdigest()
    try:
        source_mtime: float | None = Path(transcript_path).stat().st_mtime  # before the read: never newer than the bytes
    except OSError:
        source_mtime = None
    cursor_ledger = CaptureLedger(ledger.db_path)
    chunk = cursor_ledger.read_increment(transcript_path, f"dream:{provider}:{session_hash}")
    if not chunk.text:
        return {"queued": 0, "reason": "no_increment"}
    filter_key = f"filter:{provider}:{session_hash}"
    saved_filter = ledger.scan_state(filter_key)
    filter_state: dict[str, Any] = {
        "tool_contaminated": bool(saved_filter and saved_filter[0]),
        "memory_tool_ids": sorted(saved_filter[1]) if saved_filter else [],
    }
    messages = parse_transcript_lines(chunk.text, provider, state=filter_state)
    # Persist only linkage, never recalled text. Write this before advancing the
    # cursor, so replay after an interrupted capture remains conservative.
    ledger.set_scan_watermark(
        filter_key, float(bool(filter_state.get("tool_contaminated"))),
        list(filter_state.get("memory_tool_ids", ()))[:128],
    )
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
    if source_mtime is not None:
        # Also on a hook coalescing later turns into a scan-created row: M3 follows the newest write.
        ledger.record_source_mtime(capture_id, source_mtime, only_if_set=not record_source_mtime)
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


# --- AO-2 beat: incremental scan of recently modified transcripts ---------------------------

_CODEX_THREAD_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.IGNORECASE)
_PROBE_MAX_LINES = 400
_PROBE_MAX_BYTES = 1_048_576
DEFAULT_SCAN_LOOKBACK_SECONDS = 3600


def default_transcript_roots() -> dict[str, Path]:
    home = Path.home()
    return {"claude": home / ".claude" / "projects", "codex": home / ".codex" / "sessions"}


def _candidate_files(provider: str, root: Path) -> list[Path]:
    # Claude: only <project>/<session>.jsonl, the file the Stop hook reports. Sub-agent
    # transcripts live deeper and carry their parent's sessionId, which would share (and
    # keep resetting) the parent's cursor.
    pattern = "*/*.jsonl" if provider == "claude" else "**/*.jsonl"
    return [path for path in root.glob(pattern) if path.is_file()]


def _probe_session(path: Path, provider: str) -> tuple[str, str | None]:
    """Session id (the hook's cursor key) and working directory of a transcript.

    The id must equal what the lifecycle hook sends, or the beat and the hook would
    read the same bytes under two cursors: Claude names the file after its session,
    Codex records the thread id in the first ``session_meta`` record.
    """
    session_id = path.stem
    cwd: str | None = None
    if provider == "codex":
        match = _CODEX_THREAD_ID.search(path.stem)
        if match:
            session_id = match.group(1)
    read = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for index, raw in enumerate(handle):
            # Parse the line that crosses the byte cap (a first line with an embedded image is
            # common) and only then stop; the caps bound how many LINES are read, not their parsing.
            if index >= _PROBE_MAX_LINES or read > _PROBE_MAX_BYTES:
                break
            read += len(raw)
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(entry, dict):
                continue
            if provider == "codex":
                payload = entry.get("payload")
                if entry.get("type") == "session_meta" and isinstance(payload, dict):
                    session_id = str(payload.get("id") or session_id)
                    cwd = str(payload.get("cwd") or "") or None
                    break
            else:
                found = entry.get("cwd")
                if isinstance(found, str) and found:
                    cwd = found
                    break
    return session_id, cwd


def scan_active_transcripts(
    ledger: DreamLedger,
    *,
    max_files: int = 10,
    roots: Mapping[str, str | Path] | None = None,
    now: datetime | None = None,
    lookback_seconds: int = DEFAULT_SCAN_LOOKBACK_SECONDS,
) -> dict[str, Any]:
    """Capture the new bytes of transcripts modified since the last scan (AO-2 beat).

    The per-provider watermark (mtime of the newest file handled) lives in the ledger
    state db. Files are handled oldest first, at most ``max_files`` per call, so a
    backlog drains over successive ticks. A failure stops that provider at the failing
    file: the watermark never moves past work that was not done. A first scan only
    looks back ``lookback_seconds``; the byte cursor itself makes a re-scan harmless.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    resolved = {name: Path(path) for name, path in (roots if roots is not None else default_transcript_roots()).items()}
    summary: dict[str, Any] = {
        "scanned": 0, "queued": 0, "no_increment": 0, "skipped_no_cwd": 0, "errors": 0, "by_provider": {},
    }
    pending: list[tuple[float, str, Path]] = []
    states: dict[str, tuple[float, frozenset[str]]] = {}
    for provider in ("claude", "codex"):
        root = resolved.get(provider)
        if root is None or not root.is_dir():
            continue
        key = f"scan:{provider}"
        state = ledger.scan_state(key)
        if state is None:
            state = ((current - timedelta(seconds=max(0, lookback_seconds))).timestamp(), frozenset())
            ledger.set_scan_watermark(key, state[0])
        states[provider] = state
        mark, seen = state
        for path in _candidate_files(provider, root):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            # Files AT the watermark mtime that were not handled yet (a max_files cut split a tie) stay pending.
            if mtime > mark or (mtime == mark and str(path) not in seen):
                pending.append((mtime, provider, path))
    pending.sort(key=lambda item: (item[0], str(item[2])))
    blocked: set[str] = set()
    advanced: dict[str, list[tuple[float, str]]] = {}
    attempts = 0
    for mtime, provider, path in pending:
        # A failing provider is skipped BEFORE it can use up the tick's budget, so its backlog of
        # older files never starves the other provider.
        if provider in blocked:
            continue
        if attempts >= max(0, max_files):
            break
        attempts += 1
        stats = summary["by_provider"].setdefault(provider, {"scanned": 0, "queued": 0})
        try:
            session_id, cwd = _probe_session(path, provider)
            if cwd is None:
                summary["skipped_no_cwd"] += 1
            else:
                result = capture_transcript(
                    path, provider=provider, session_id=session_id, cwd=cwd, ledger=ledger, record_source_mtime=True,
                )
                if result.get("queued"):
                    summary["queued"] += 1
                    stats["queued"] += 1
                else:
                    summary["no_increment"] += 1
        except Exception:  # noqa: BLE001 - one bad transcript must not stop the tick or lose the others
            summary["errors"] += 1
            blocked.add(provider)
            continue
        summary["scanned"] += 1
        stats["scanned"] += 1
        advanced.setdefault(provider, []).append((mtime, str(path)))
    for provider, handled in advanced.items():
        old_mark, old_seen = states[provider]
        new_mark = max(old_mark, *(mtime for mtime, _ in handled))
        seen = {path for mtime, path in handled if mtime == new_mark}
        ledger.set_scan_watermark(f"scan:{provider}", new_mark, seen | (old_seen if new_mark == old_mark else set()))
    return summary
