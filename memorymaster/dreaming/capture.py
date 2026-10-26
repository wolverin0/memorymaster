"""Fast transcript adapters used by native agent lifecycle hooks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping

from memorymaster.core.capture_control import CaptureLedger
from memorymaster.core.generated_artifacts import SourceSpan
from memorymaster.core.security import redact_text
from memorymaster.core.scope_utils import resolve_transcript_workspace_scope, scope_from_cwd
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
) -> tuple[str, tuple[SourceSpan, ...]] | None:
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
    # The convenience joined text is never attributable evidence: a removed
    # range can separate an authored prefix from an unrelated suffix.
    return (normalized_role, clean.spans) if normalized_role and clean.spans else None


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
        role, spans = record
        timestamp = str(entry.get("timestamp") or _now())
        for span in spans:
            sanitized = _sanitize(span.text)
            if not sanitized:
                continue
            identity = (role, sanitized, timestamp)
            if identity in seen:
                continue
            seen.add(identity)
            raw_id = str(entry.get("uuid") or entry.get("id") or "|".join(identity))
            if len(spans) > 1:
                raw_id += f":span:{span.start}:{span.end}"
            message_id = "dm-" + hashlib.sha256(raw_id.encode("utf-8", errors="replace")).hexdigest()[:20]
            messages.append(DreamMessage(message_id, role, sanitized, timestamp))
    return messages


def _scope(cwd: str | None) -> str | None:
    return resolve_transcript_workspace_scope(cwd)


def _capture_agy_transcript(
    transcript_path: str | Path, *, session_id: str, cwd: str | None,
    ledger: DreamLedger, record_source_mtime: bool, live_workspace: bool,
) -> dict[str, Any]:
    # agy_source shares our sanitizer; keep this import lazy to avoid a cycle.
    from memorymaster.dreaming.agy_source import AgySourceError, iter_agy_records

    scope = scope_from_cwd(cwd) if live_workspace and cwd else _scope(cwd)
    if scope is None or scope == "global":
        return {"queued": 0, "reason": "unresolved_scope"}
    session_hash = hashlib.sha256((session_id or "unknown").encode("utf-8", errors="replace")).hexdigest()
    path = Path(transcript_path)
    try:
        stat = path.stat()
        full = path.with_name("transcript_full.jsonl")
        full_end = full.stat().st_size if full.is_file() else 0
        # Replay the bounded latest-step view, including its filtering state.
        # A byte-only cursor would miss edits to old steps or full replacements.
        records = list(iter_agy_records(path, frozen_end=stat.st_size, full_frozen_end=full_end))
    except (AgySourceError, OSError, ValueError, UnicodeError):
        return {"queued": 0, "reason": "invalid_transcript"}
    if not records:
        return {"queued": 0, "reason": "no_conversation"}
    if len(records) < 2:
        # The worker requires two turns. Leave fingerprints untouched so a
        # later Stop can capture the first turn together with its response.
        return {"queued": 0, "reason": "insufficient_turns"}

    state_key = f"agy-revisions:{session_hash}:{scope}"
    state = ledger.scan_state(state_key)
    previous = state[1] if state else frozenset()
    # At most one digest per eligible step, bounded by agy_source.MAX_STEPS.
    # Never persist raw source text or workspace paths in scan bookkeeping.
    fingerprints = [
        f"{record.step_index}:" + hashlib.sha256(
            json.dumps(record.message.to_dict(), sort_keys=True).encode("utf-8"),
        ).hexdigest()
        for record in records
    ]
    changed = [record for record, fingerprint in zip(records, fingerprints) if fingerprint not in previous]
    if not changed:
        return {"queued": 0, "reason": "no_increment"}
    selected = changed
    if len(changed) == 1:
        # One revision alone cannot pass the worker's two-turn gate. Put its
        # nearest authored context first so tail packing retains the revision.
        index = records.index(changed[0])
        context = records[index - 1] if index else records[1]
        selected = [context, changed[0]]
    messages = tuple(record.message for record in selected)
    content_hash = hashlib.sha256(json.dumps([m.to_dict() for m in messages], sort_keys=True).encode("utf-8")).hexdigest()
    envelope = CaptureEnvelope(
        provider="antigravity", session_hash=session_hash, scope=scope, captured_at=_now(),
        last_activity_at=messages[-1].timestamp or _now(), messages=messages,
        cursor_start=min(record.start_offset for record in selected),
        cursor_end=max(record.end_offset for record in selected), content_hash=content_hash,
    )
    # Coalescing deduplicates by message ID and would discard revised steps.
    # Enqueue before the watermark: interrupted replay has the same capture key.
    capture_id = ledger.enqueue(envelope, coalesce=False)
    ledger.set_scan_watermark(state_key, stat.st_mtime, fingerprints)
    ledger.record_source_mtime(capture_id, stat.st_mtime, only_if_set=not record_source_mtime)
    return {"queued": 1, "capture_id": capture_id, "messages": len(messages)}


def _in_claude_cli_scratch(cwd: str | None) -> bool:
    """True for sessions run by MemoryMaster's own `claude -p` calls (extraction, steward).

    They must never be captured: extracting a capture of an extraction would feed itself.
    """
    if not cwd:
        return False
    scratch = os.environ.get("MEMORYMASTER_CLAUDE_CLI_CWD", "").strip() or str(
        Path.home() / ".memorymaster" / "claude_cli_scratch"
    )
    root, session = (os.path.normcase(os.path.abspath(p)) for p in (scratch, cwd))
    return session == root or session.startswith(root + os.sep)


def capture_transcript(
    transcript_path: str | Path, *, provider: str, session_id: str, cwd: str | None, ledger: DreamLedger,
    record_source_mtime: bool = False,
    live_workspace: bool = False,
) -> dict[str, Any]:
    """Queue new transcript evidence (one envelope per changed batch).

    ``record_source_mtime`` is the beat scan's: it sets the capture's source mtime. Lifecycle hooks
    leave it off, so they never create beat data (an inactive beat stays invisible to the review);
    they only refresh a source mtime the beat already set when they coalesce into that capture.
    """
    if _in_claude_cli_scratch(cwd):
        return {"queued": 0, "reason": "internal_scratch"}
    if provider.lower() == "antigravity":
        return _capture_agy_transcript(
            transcript_path, session_id=session_id, cwd=cwd, ledger=ledger,
            record_source_mtime=record_source_mtime, live_workspace=live_workspace,
        )
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
    # A native lifecycle payload identifies its active workspace, including
    # non-Git projects. Historical scans still require repository/alias evidence.
    scope = scope_from_cwd(cwd) if live_workspace and cwd else _scope(cwd)
    if scope is None or scope == "global":
        return {"queued": 0, "reason": "unresolved_scope"}
    content_hash = hashlib.sha256(json.dumps([m.to_dict() for m in messages], sort_keys=True).encode("utf-8")).hexdigest()
    envelope = CaptureEnvelope(
        provider=provider.lower(), session_hash=session_hash, scope=scope, captured_at=_now(),
        last_activity_at=messages[-1].timestamp or _now(), messages=tuple(messages),
        cursor_start=chunk.start_offset, cursor_end=chunk.end_offset, content_hash=content_hash,
    )
    capture_id = ledger.enqueue(envelope)
    cursor_ledger.commit_cursor(chunk)
    if source_mtime is not None:
        # Also on a hook coalescing later turns into a scan-created row: M3 follows the newest write.
        ledger.record_source_mtime(capture_id, source_mtime, only_if_set=not record_source_mtime)
    return {"queued": 1, "capture_id": capture_id, "messages": len(messages)}


_OPENCODE_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_OPENCODE_MAX_RECORDS = 10_000
_OPENCODE_MAX_JSON_BYTES = 2_097_152


def _opencode_json(raw: str) -> dict[str, Any]:
    if len(raw.encode("utf-8")) > _OPENCODE_MAX_JSON_BYTES:
        raise ValueError("oversized OpenCode record")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid OpenCode record")
    return value


def _opencode_rows(cursor: sqlite3.Cursor) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    size = 0
    for row in cursor:
        size += sum(len(value) for value in row if isinstance(value, str))
        if len(rows) >= _OPENCODE_MAX_RECORDS or size > 32 * 1_048_576:
            raise ValueError("oversized OpenCode session")
        rows.append(row)
    return rows


def _opencode_sqlite(path: Path, session_id: str) -> tuple[str, list[dict[str, Any]]] | None:
    # Primary storage contracts (v1 and current v2):
    # https://github.com/anomalyco/opencode/blob/v1.2.0/packages/opencode/src/session/session.sql.ts
    # https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/sql.ts
    # mode=ro never creates/migrates the producer DB; BEGIN gives one WAL snapshot.
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("session_v2", "session"):
            if table not in tables:
                continue
            session = conn.execute(f"SELECT directory FROM {table} WHERE id=?", (session_id,)).fetchone()
            if session is None:
                continue
            records: list[dict[str, Any]] = []
            if "session_message" in tables:
                rows = _opencode_rows(conn.execute(
                    "SELECT id,type,time_created,data FROM session_message WHERE session_id=? ORDER BY seq,id",
                    (session_id,),
                ))
                for row in rows:
                    data = _opencode_json(row["data"])
                    records.append({"id": row["id"], "role": row["type"], "created": row["time_created"], **{
                        "parts": data.get("content", []) if row["type"] == "assistant" else [
                            {"type": "text", "text": data.get("text", "")},
                        ],
                        "generated": row["type"] not in {"user", "assistant"} or data.get("summary") is True,
                    }})
                # A session awaiting v1 migration may still have only old rows.
                if rows or table == "session_v2":
                    return str(session["directory"]), records
            if not {"message", "part"} <= tables:
                return str(session["directory"]), []
            messages = _opencode_rows(conn.execute(
                "SELECT id,time_created,data FROM message WHERE session_id=? ORDER BY time_created,id", (session_id,),
            ))
            parts: dict[str, list[dict[str, Any]]] = {}
            for row in _opencode_rows(conn.execute(
                "SELECT message_id,data FROM part WHERE session_id=? ORDER BY id", (session_id,),
            )):
                parts.setdefault(row["message_id"], []).append(_opencode_json(row["data"]))
            for row in messages:
                info = _opencode_json(row["data"])
                records.append({"id": row["id"], "role": info.get("role"), "created": row["time_created"],
                                "parts": parts.get(row["id"], []), "generated": info.get("summary") is True})
            return str(session["directory"]), records
    return None


def _opencode_file(path: Path, root: Path) -> dict[str, Any]:
    # Reject symlink escapes as well as traversal in producer-owned identifiers.
    path.resolve().relative_to(root.resolve())
    with path.open("r", encoding="utf-8") as handle:
        return _opencode_json(handle.read(_OPENCODE_MAX_JSON_BYTES + 1))


def _opencode_legacy(root: Path, session_id: str) -> tuple[str, list[dict[str, Any]]] | None:
    # Legacy layout and field names are defined by OpenCode's own migration:
    # https://github.com/anomalyco/opencode/blob/v1.2.0/packages/opencode/src/storage/json-migration.ts
    storage = root / "storage"
    sessions = list(storage.glob(f"session/*/{session_id}.json"))
    if not sessions:
        return None
    if len(sessions) != 1:
        raise ValueError("ambiguous OpenCode session")
    session = _opencode_file(sessions[0], storage)
    if session.get("id") != session_id:
        raise ValueError("OpenCode session identity mismatch")
    records: list[dict[str, Any]] = []
    for path in (storage / "message" / session_id).glob("*.json"):
        if len(records) >= _OPENCODE_MAX_RECORDS:
            raise ValueError("oversized OpenCode session")
        info = _opencode_file(path, storage)
        message_id = info.get("id")
        if not isinstance(message_id, str) or not _OPENCODE_ID.fullmatch(message_id):
            raise ValueError("invalid OpenCode message id")
        if info.get("sessionID", session_id) != session_id:
            raise ValueError("OpenCode message session mismatch")
        parts = []
        for part_path in sorted((storage / "part" / message_id).glob("*.json")):
            if len(parts) >= _OPENCODE_MAX_RECORDS:
                raise ValueError("oversized OpenCode message")
            part = _opencode_file(part_path, storage)
            if part.get("sessionID", session_id) != session_id or part.get("messageID", message_id) != message_id:
                raise ValueError("OpenCode part identity mismatch")
            parts.append(part)
        clock = info.get("time")
        records.append({"id": message_id, "role": info.get("role"), "parts": parts,
                        "created": clock.get("created", 0) if isinstance(clock, dict) else 0,
                        "generated": info.get("summary") is True})
    records.sort(key=lambda row: (row["created"], row["id"]))
    return str(session.get("directory") or ""), records


def _opencode_messages(records: list[dict[str, Any]]) -> list[DreamMessage]:
    # v2 authored user.text and assistant.content are defined here:
    # https://github.com/anomalyco/opencode/blob/dev/packages/schema/src/session-message.ts
    # Replay structural state from the beginning, so an idle boundary cannot
    # erase the linkage from a memory tool to its later assistant echo.
    state: dict[str, Any] = {}
    messages: list[DreamMessage] = []
    for record in records:
        role = record.get("role")
        parts = record.get("parts")
        if not isinstance(parts, list):
            raise ValueError("invalid OpenCode content")
        injected = bool(record.get("generated"))
        memory_call = False
        for part in parts:
            if not isinstance(part, dict):
                raise ValueError("invalid OpenCode part")
            injected |= bool(part.get("synthetic") or part.get("ignored"))
            name = str(part.get("tool") or part.get("name") or "")
            # OpenCode names MCP tools <server>_<tool>; the built-in filter
            # also accepts Claude/Codex's mcp__memorymaster__ namespace.
            if part.get("type") == "tool" and (is_memory_tool(name) or name.lower().startswith("memorymaster_")):
                memory_call = True
                strip_self_observation("", role="tool", tool_name="mcp__memorymaster__recall", metadata={
                    "state": state, "tool_call_id": str(part.get("callID") or part.get("id") or ""),
                })
        if memory_call:
            continue
        text = "\n".join(part["text"] for part in parts if part.get("type") == "text" and isinstance(part.get("text"), str))
        clean = strip_self_observation(text, role=str(role), metadata={"state": state, "injected": injected})
        if role not in {"user", "assistant"}:
            continue
        timestamp = datetime.fromtimestamp(float(record.get("created") or 0) / 1000, timezone.utc).isoformat()
        for span in clean.spans:
            sanitized = _sanitize(span.text)
            if sanitized:
                # Include the revision: a producer can update a text part in place.
                identity = f"{record['id']}:{span.start}:{span.end}:{sanitized}"
                message_id = "dm-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
                messages.append(DreamMessage(message_id, str(role), sanitized, timestamp))
    return messages


def _capture_opencode_session(
    session_id: str, *, cwd: str | None, ledger: DreamLedger, data_dir: str | Path | None,
) -> dict[str, Any]:
    if not isinstance(session_id, str) or not _OPENCODE_ID.fullmatch(session_id):
        return {"queued": 0, "reason": "invalid_session"}
    if not cwd or scope_from_cwd(cwd) == "global":
        return {"queued": 0, "reason": "unresolved_scope"}
    try:
        root = Path(data_dir) if data_dir is not None else Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "opencode"
        override = os.environ.get("OPENCODE_DB") if data_dir is None else None
        database = (root / override) if override else root / "opencode.db"
        source = _opencode_sqlite(database, session_id) if database.is_file() else _opencode_legacy(root, session_id)
        if source is None:
            return {"queued": 0, "reason": "session_not_found"}
        directory, records = source
        if not directory or Path(directory).resolve() != Path(cwd).resolve():
            return {"queued": 0, "reason": "workspace_mismatch"}
        messages = _opencode_messages(records)
    except (OSError, ValueError, TypeError, OverflowError, sqlite3.Error):
        return {"queued": 0, "reason": "storage_unavailable"}
    session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    scope = scope_from_cwd(cwd)
    state_key = f"opencode:messages:{session_hash}:{scope}"
    state = ledger.scan_state(state_key)
    start, seen = (int(state[0]), state[1]) if state else (0, frozenset())
    fresh = [message for message in messages if message.message_id not in seen]
    if not fresh:
        return {"queued": 0, "reason": "no_increment"}
    end = start + len(fresh)
    content_hash = hashlib.sha256(json.dumps([m.to_dict() for m in fresh], sort_keys=True).encode("utf-8")).hexdigest()
    envelope = CaptureEnvelope(
        provider="opencode", session_hash=session_hash, scope=scope, captured_at=_now(),
        last_activity_at=fresh[-1].timestamp, messages=tuple(fresh), cursor_start=start, cursor_end=end,
        content_hash=content_hash,
    )
    capture_id = ledger.enqueue(envelope)
    # Advance only after enqueue; interruption replays the exact same envelope.
    ledger.set_scan_watermark(state_key, end, seen | {message.message_id for message in fresh})
    return {"queued": 1, "capture_id": capture_id, "messages": len(fresh)}


def capture_opencode_session(
    session_id: str, *, cwd: str | None, ledger: DreamLedger, data_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Read one native OpenCode session without scanning other providers or writing its store."""
    from memorymaster.surfaces.hook_events import append_hook_event

    result = _capture_opencode_session(session_id, cwd=cwd, ledger=ledger, data_dir=data_dir)
    append_hook_event("opencode", "capture", int(result.get("messages", 0)))
    return result


def capture_hook_payload(payload: dict[str, Any], *, provider: str, ledger: DreamLedger) -> dict[str, Any]:
    if provider.lower() == "antigravity":
        workspaces = payload.get("workspacePaths")
        if not isinstance(workspaces, list) or len(workspaces) != 1:
            return {"queued": 0, "reason": "unresolved_scope"}
        cwd = workspaces[0]
        if not isinstance(cwd, str) or not cwd.strip() or not (
            PureWindowsPath(cwd).is_absolute() or PurePosixPath(cwd).is_absolute()
        ):
            return {"queued": 0, "reason": "unresolved_scope"}
        transcript_path = payload.get("transcriptPath")
        if not isinstance(transcript_path, str) or not transcript_path.strip():
            return {"queued": 0, "reason": "no_transcript"}
        session_id = payload.get("conversationId")
        if not isinstance(session_id, str) or not session_id.strip():
            return {"queued": 0, "reason": "no_session"}
        return capture_transcript(
            transcript_path, provider="antigravity", session_id=session_id,
            cwd=cwd, ledger=ledger, live_workspace=True,
        )
    if provider.lower() == "opencode":
        return capture_opencode_session(
            str(payload.get("session_id") or ""), cwd=str(payload.get("cwd") or "") or None,
            ledger=ledger, data_dir=payload.get("data_dir"),
        )
    transcript_path = str(payload.get("transcript_path") or "")
    if not transcript_path:
        return {"queued": 0, "reason": "no_transcript"}
    return capture_transcript(
        transcript_path,
        provider=provider,
        session_id=str(payload.get("session_id") or payload.get("thread_id") or "unknown"),
        cwd=str(payload.get("cwd") or "") or None,
        ledger=ledger,
        live_workspace=True,
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
