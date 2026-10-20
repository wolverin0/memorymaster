"""Bounded read-only adapters for supplied agy exports and session families.

No default home root is discovered. JSONL is evidence; executor_metadata is
used only for workspace/model identity. Unsupported protobuf shapes fail closed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterator

from memorymaster.dreaming.capture import _sanitize
from memorymaster.dreaming.models import DreamMessage
from memorymaster.dreaming.self_observation import strip_self_observation


MAX_METADATA_BYTES = 1_048_576
MAX_METADATA_ROWS = 4096
MAX_RECORD_BYTES = 1_048_576
MAX_STEPS = 200_000
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


@dataclass(frozen=True)
class ExecutorMetadata:
    cwd: str | None = None
    model_id: str | None = None
    status: str = "unresolved_scope"
    reason: str = "missing_metadata"
    model_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgySession:
    uuid: str
    transcript_path: Path
    full_path: Path | None
    database_path: Path | None


@dataclass(frozen=True)
class AgyRecord:
    message: DreamMessage
    step_index: int
    start_offset: int
    end_offset: int
    source_type: str
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True)
class SessionBundle:
    provider: str
    session_id: str
    streams: tuple[Path, ...]
    child_streams: tuple[Path, ...] = ()


class AgySourceError(ValueError):
    """A visible disposition; callers must not report a partial scan complete."""


def _varint(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if position >= len(data):
            break
        byte = data[position]
        position += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, position
    raise ValueError("invalid protobuf varint")


def _fields(data: bytes) -> dict[int, list[bytes]]:
    if len(data) > MAX_METADATA_BYTES:
        raise ValueError("metadata size bound")
    result: dict[int, list[bytes]] = {}
    position = 0
    count = 0
    while position < len(data):
        count += 1
        if count > 10_000:
            raise ValueError("metadata field bound")
        tag, position = _varint(data, position)
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ValueError("invalid protobuf field")
        if wire == 0:
            _, position = _varint(data, position)
        elif wire in {1, 5}:
            position += 8 if wire == 1 else 4
        elif wire == 2:
            length, position = _varint(data, position)
            end = position + length
            if end > len(data):
                raise ValueError("truncated protobuf field")
            result.setdefault(number, []).append(data[position:end])
            position = end
        else:
            raise ValueError("unsupported protobuf wire")
        if position > len(data):
            raise ValueError("truncated protobuf scalar")
    return result


def _decode_path(data: bytes, path: tuple[int, ...]) -> list[str]:
    values = [data]
    for number in path:
        values = [item for value in values for item in _fields(value).get(number, [])]
        if len(values) > 64:
            raise ValueError("metadata fanout bound")
    strings = [value.decode("utf-8", errors="strict").strip() for value in values]
    if any(len(value) > 4096 or "\x00" in value for value in strings):
        raise ValueError("invalid metadata string")
    return [value for value in strings if value]


def read_executor_metadata(db_path: str | Path | None) -> ExecutorMetadata:
    """Read only bounded identity fields with both SQLite read-only barriers."""
    if db_path is None or not Path(db_path).is_file():
        return ExecutorMetadata(reason="missing_companion")
    conn = None
    try:
        conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(
            "SELECT idx, length(data), CASE WHEN length(data) <= ? THEN data END "
            "FROM executor_metadata ORDER BY idx LIMIT ?", (MAX_METADATA_BYTES, MAX_METADATA_ROWS + 1),
        )
        cwds: dict[str, str] = {}
        models: list[str] = []
        scanned = 0
        total = 0
        for _, length, blob in rows:
            scanned += 1
            total += length or 0
            if scanned > MAX_METADATA_ROWS or total > MAX_METADATA_BYTES * 4 or not isinstance(blob, bytes):
                raise ValueError("metadata bounds or shape")
            for cwd in _decode_path(blob, (10, 1, 42, 11, 1)):
                if not (PureWindowsPath(cwd).is_absolute() or PurePosixPath(cwd).is_absolute()):
                    raise ValueError("nonabsolute metadata cwd")
                normalized = str(PureWindowsPath(cwd)).casefold() if PureWindowsPath(cwd).drive else str(PurePosixPath(cwd))
                cwds[normalized] = cwd
            for model in _decode_path(blob, (10, 1, 28)):
                if model not in models:
                    models.append(model)
        if len(cwds) != 1:
            return ExecutorMetadata(reason="conflicting_cwd" if cwds else "missing_cwd", model_ids=tuple(models))
        return ExecutorMetadata(next(iter(cwds.values())), models[-1] if models else None, "resolved", "executor_metadata", tuple(models))
    except (OSError, sqlite3.Error, ValueError, UnicodeError):
        return ExecutorMetadata(reason="decoder_failure")
    finally:
        if conn is not None:
            conn.close()


read_agy_workspace_metadata = read_executor_metadata


def discover_agy_sessions(root: str | Path) -> Iterator[AgySession]:
    """Only canonical compact logs; full/chunk representations are companions."""
    source_root = Path(root)
    brain = source_root / "brain" if (source_root / "brain").is_dir() else source_root
    conversations = source_root / "conversations" if brain != source_root else source_root.parent / "conversations"
    if not brain.is_dir():
        return
    for directory in sorted(brain.iterdir()):
        if not directory.is_dir() or not _UUID.fullmatch(directory.name):
            continue
        logs = directory / ".system_generated" / "logs"
        compact, full = logs / "transcript.jsonl", logs / "transcript_full.jsonl"
        database = conversations / f"{directory.name}.db"
        if compact.is_file():
            yield AgySession(directory.name, compact, full if full.is_file() else None, database if database.is_file() else None)


def discover_session_bundles(root: str | Path, *, provider: str = "claude") -> Iterator[SessionBundle]:
    """Discover parent/child Claude streams with one independent session budget."""
    source_root = Path(root)
    if provider == "agy":
        for session in discover_agy_sessions(source_root):
            yield SessionBundle("agy", session.uuid, (session.transcript_path,))
        return
    if provider != "claude":
        raise ValueError("bundle discovery supports claude and agy")
    for parent in sorted(source_root.glob("*/*.jsonl")):
        if not parent.is_file():
            continue
        children = tuple(sorted(path for path in (parent.parent / parent.stem / "subagents").glob("*.jsonl") if path.is_file()))
        yield SessionBundle("claude", parent.stem, (parent, *children), children)


def _latest_offsets(path: Path, frozen_end: int | None) -> dict[int, tuple[int, int]]:
    offsets: dict[int, tuple[int, int]] = {}
    limit = path.stat().st_size if frozen_end is None else frozen_end
    if limit < 0 or limit > path.stat().st_size:
        raise AgySourceError("invalid_frozen_end")
    with path.open("rb") as handle:
        while handle.tell() < limit:
            start = handle.tell()
            raw = handle.readline(min(MAX_RECORD_BYTES + 1, limit - start))
            end = handle.tell()
            if len(raw) > MAX_RECORD_BYTES:
                raise AgySourceError("oversized_record")
            if not raw.endswith(b"\n"):
                break  # Appended/trailing partial bytes are never evidence.
            try:
                row = json.loads(raw)
                step = row.get("step_index") if isinstance(row, dict) else None
                if type(step) is not int or step < 0:
                    raise ValueError
            except (ValueError, UnicodeError):
                raise AgySourceError("malformed_record") from None
            offsets[step] = (start, end)
            if len(offsets) > MAX_STEPS:
                raise AgySourceError("step_count_bound")
    return offsets


def _read_row(handle: Any, offsets: tuple[int, int]) -> dict[str, Any]:
    start, end = offsets
    handle.seek(start)
    return json.loads(handle.read(end - start))


def iter_agy_records(
    transcript_path: str | Path, *, full_path: str | Path | None = None,
    frozen_end: int | None = None, full_frozen_end: int | None = None,
) -> Iterator[AgyRecord]:
    """Latest step revisions, bounded full-field replacement and normalized roles.

    Byte offset manifests refer to the compact source. Missing full replacements
    are explicit errors, never a silently accepted truncated evidence quote.
    """
    compact = Path(transcript_path)
    full = Path(full_path) if full_path else compact.with_name("transcript_full.jsonl")
    latest = _latest_offsets(compact, frozen_end)
    full_latest = _latest_offsets(full, full_frozen_end) if full.is_file() else {}
    session_id = compact.parents[2].name if compact.name == "transcript.jsonl" else compact.stem
    state: dict[str, Any] = {}
    with compact.open("rb") as handle:
        full_handle = full.open("rb") if full.is_file() else None
        try:
            for step, offsets in sorted(latest.items()):
                row = _read_row(handle, offsets)
                source_type = str(row.get("type", ""))
                source = row.get("source")
                truncated = row.get("truncated_fields", [])
                if not isinstance(truncated, list):
                    raise AgySourceError("invalid_truncated_fields")
                eligible_fields = {"error"}
                if source_type in {"USER_INPUT", "PLANNER_RESPONSE", "ERROR_MESSAGE"}:
                    eligible_fields.add("content")
                if source_type == "PLANNER_RESPONSE":
                    eligible_fields.add("thinking")
                wanted = {name for name in truncated if isinstance(name, str) and name in eligible_fields}
                provenance: list[str] = []
                if wanted:
                    if full_handle is None or step not in full_latest:
                        raise AgySourceError("missing_full_fields")
                    replacement = _read_row(full_handle, full_latest[step])
                    if any(not isinstance(replacement.get(name), str) for name in wanted):
                        raise AgySourceError("missing_full_fields")
                    row.update({name: replacement[name] for name in wanted})
                    provenance.append("full_field_replacement")
                role = "assistant"
                parts: list[str] = []
                content = row.get("content") if isinstance(row.get("content"), str) else ""
                if source_type == "USER_INPUT" and source == "USER_EXPLICIT":
                    role = "user"
                    parts.append(content)
                elif source_type == "PLANNER_RESPONSE" and source == "MODEL":
                    parts.append(content)
                    thinking = row.get("thinking")
                    if len(content.strip()) < 120 and isinstance(thinking, str) and thinking.strip():
                        parts.append("[thinking] " + thinking[:2000])
                        provenance.append("model_reasoning")
                elif source_type == "ERROR_MESSAGE":
                    if content:
                        parts.append("[error] " + content)
                    provenance.append("historical_error")
                error = row.get("error")
                if isinstance(error, str) and error.strip():
                    parts.append("[error] " + error)
                    provenance.append("historical_error")
                # Tool args/results remain excluded, but memory-tool structural
                # origin still contaminates following model turns.
                tools = row.get("tool_calls", [])
                for tool in tools if isinstance(tools, list) else []:
                    if isinstance(tool, dict):
                        strip_self_observation("", role="tool", tool_name=str(tool.get("name", "")), metadata={"state": state})
                if not parts:
                    continue
                clean = strip_self_observation("\n".join(parts), role=role, metadata={"state": state})
                text = _sanitize(clean.text)
                if not text:
                    continue
                identity = f"agy:{session_id}:{step}"
                message = DreamMessage("dm-" + hashlib.sha256(identity.encode()).hexdigest()[:20], role, text, str(row.get("created_at") or ""))
                yield AgyRecord(message, step, *offsets, source_type, tuple(provenance))
        finally:
            if full_handle is not None:
                full_handle.close()


def extract_agy_session(transcript_path: str | Path, **kwargs: Any) -> tuple[DreamMessage, ...]:
    return tuple(record.message for record in iter_agy_records(transcript_path, **kwargs))
