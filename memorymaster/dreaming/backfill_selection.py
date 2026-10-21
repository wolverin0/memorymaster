"""Bounded historical selection. Raw locators stay in the private auxiliary ledger."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from memorymaster.dreaming.models import DreamCandidate, DreamMessage
from memorymaster.dreaming.source_review import bind_source

MAX_RECORD_BYTES = 2 * 1024 * 1024
WINDOW_CHARS = 4_000  # Also leaves room for source wrapping and transport framing.
SELECTOR_VERSION = "v2"


@dataclass(frozen=True)
class SourceTurn:
    message: DreamMessage
    start: int
    end: int
    stream_id: str


def complete_records(path: Path, *, frozen_end: int | None = None) -> Iterator[tuple[int, int, bytes | None]]:
    """Consume oversized lines in bounded pieces, and never read a partial last line."""
    with path.open("rb") as handle:
        end = path.stat().st_size if frozen_end is None else frozen_end
        while handle.tell() < end:
            start = handle.tell()
            raw = handle.readline(min(MAX_RECORD_BYTES + 1, end - start))
            oversized = len(raw) > MAX_RECORD_BYTES
            complete = raw.endswith(b"\n")
            while not complete and handle.tell() < end:
                oversized = True
                part = handle.readline(min(65536, end - handle.tell()))
                complete = part.endswith(b"\n")
            if not complete:
                break
            yield start, handle.tell(), None if oversized else raw


def frozen_identity(path: Path) -> tuple[int, str]:
    """Hash only a complete-line prefix without allocating a source-sized buffer."""
    limit = path.stat().st_size
    end = 0
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while handle.tell() < limit:
            piece = handle.read(min(65536, limit - handle.tell()))
            if not piece:
                break
            last = piece.rfind(b"\n")
            if last >= 0:
                end = handle.tell() - len(piece) + last + 1
    with path.open("rb") as handle:
        remaining = end
        while remaining:
            piece = handle.read(min(65536, remaining))
            if not piece:
                raise ValueError("source_truncated")
            digest.update(piece)
            remaining -= len(piece)
    return end, digest.hexdigest()


def verify_frozen(path: Path, end: int, expected: str) -> bool:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        remaining = end
        while remaining:
            piece = handle.read(min(65536, remaining))
            if not piece:
                return False
            digest.update(piece)
            remaining -= len(piece)
    return digest.hexdigest() == expected


def valid_timestamp(value: Any, *, now: datetime | None = None) -> str | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        stamp = stamp.astimezone(timezone.utc)
        if datetime(2025, 1, 1, tzinfo=timezone.utc) <= stamp <= (now or datetime.now(timezone.utc)) + timedelta(days=1):
            return stamp.isoformat()
    except (ValueError, TypeError):
        pass
    return None


def iter_source_turns(path: Path, provider: str, stream_id: str, frozen_end: int,
                      gaps: list[dict[str, Any]]) -> Iterator[SourceTurn]:
    from memorymaster.dreaming.capture import parse_transcript_lines

    if provider == "agy":
        from memorymaster.dreaming.agy_source import iter_agy_records

        for record in iter_agy_records(path, frozen_end=frozen_end):
            yield SourceTurn(record.message, record.start_offset, record.end_offset, stream_id)
        return
    state: dict[str, Any] = {}
    for start, end, raw in complete_records(path, frozen_end=frozen_end):
        if raw is None:
            gaps.append({"stream": stream_id, "start": start, "end": end, "reason": "oversized_record"})
            continue
        try:
            entry = json.loads(raw)
            if not isinstance(entry, dict):
                raise ValueError("record_not_object")
        except (ValueError, UnicodeError):
            gaps.append({"stream": stream_id, "start": start, "end": end, "reason": "malformed"})
            continue
        for message in parse_transcript_lines(json.dumps(entry), provider, state=state):
            yield SourceTurn(message, start, end, stream_id)


def _trim(turn: SourceTurn) -> SourceTurn:
    message = turn.message
    if len(message.text) <= WINDOW_CHARS:
        return turn
    # The slice is explicit in the identity: no fictitious adjacency is created.
    return SourceTurn(DreamMessage(message.message_id + ":span-0-4000", message.role,
                                  message.text[:WINDOW_CHARS], message.timestamp), turn.start, turn.end, turn.stream_id)


def select_windows(turns: Iterator[SourceTurn], *, scope: str) -> dict[str, Any]:
    """Keep framing, a boundary, correction/decision and tail across one parent bundle."""
    head: list[SourceTurn] = []
    tail: deque[SourceTurn] = deque()
    boundary: list[SourceTurn] = []
    decision: list[SourceTurn] = []
    eligible = 0
    count = 0
    for original in turns:
        eligible += len(original.message.text)
        count += 1
        turn = _trim(original)
        text = turn.message.text.casefold()
        if any(marker in text for marker in ("compaction", "handoff", "context limit")):
            boundary = list(tail)
        if not head or sum(len(t.message.text) for t in head) + len(turn.message.text) <= WINDOW_CHARS:
            head.append(turn)
        tail.append(turn)
        while len(tail) > 1 and sum(len(t.message.text) for t in tail) > WINDOW_CHARS:
            tail.popleft()
        if any(marker in text for marker in ("instead", "correction", "decided", "root cause", "must ")):
            decision = list(tail)
    windows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for kind, selected in (("framing", head), ("pre_boundary", boundary), ("decision", decision), ("tail", list(tail))):
        selected = [t for t in selected if (t.stream_id, t.message.message_id) not in seen]
        if not selected:
            continue
        # Test actual serialized bind_source context, never mark truncated context complete.
        while selected:
            first = selected[0].message
            candidate = DreamCandidate("window-probe", "window probe text", "lesson", "probe", "has", "window", "project", first.message_id, first.text[:50], .5)
            row = {"scope": scope, "provider": "probe", "session_hash": "probe", "messages": [t.message.to_dict() for t in selected]}
            bound = bind_source(candidate, row)
            if bound.source_context["complete"] and len(json.dumps(bound.source_context, ensure_ascii=False)) < 24_000:
                break
            selected.pop()
        if not selected:
            continue
        seen.update((t.stream_id, t.message.message_id) for t in selected)
        windows.append({"kind": kind, "messages": [t.message.to_dict() for t in selected],
                        "ranges": [{"stream": t.stream_id, "start": t.start, "end": t.end} for t in selected]})
    selected_chars = sum(len(m["text"]) for w in windows for m in w["messages"])
    return {"selector_version": SELECTOR_VERSION, "windows": windows, "eligible_chars": eligible,
            "selected_chars": selected_chars, "sampled_out_chars": eligible - selected_chars,
            "eligible_turns": count, "selected_turns": len(seen), "coverage": "sampled"}


def order_inventory(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Priority scopes first; fair rounds for all remaining resolved projects."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row.get("scope") or "unresolved", []).append(row)
    for items in groups.values():
        items.sort(key=lambda r: (r.get("internal_timestamp") or "", r["stream_id"]), reverse=True)
    result = []
    for priority in ("project:memorymaster", "project:wezbridge"):
        result.extend(groups.pop(priority, []))
    while groups:
        for scope in sorted(list(groups)):
            result.append(groups[scope].pop(0))
            if not groups[scope]:
                del groups[scope]
    return result
