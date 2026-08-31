"""Streaming adapters for retained Claude, Codex, and Wezbridge JSON/JSONL."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .models import ActionRecord, FeedbackRecord, SessionRecord, TurnRecord
from .redaction import public_excerpt


_READ_TOOLS = {"read", "grep", "glob", "find", "search", "view_image", "web__run"}
_MUTATION_TOOLS = {
    "edit", "write", "notebookedit", "apply_patch", "create_file", "delete_file",
}
_VERIFICATION_WORDS = re.compile(
    r"(?i)\b(pytest|unittest|jest|vitest|playwright|cypress|typecheck|tsc|ruff|mypy|"
    r"npm\s+(?:run\s+)?(?:test|build)|pnpm\s+(?:test|build)|cargo\s+test|go\s+test)\b"
)
_MUTATION_COMMANDS = re.compile(
    r"(?i)\b(?:sed\s+-i|git\s+(?:commit|add|merge|rebase|push)|npm\s+install|"
    r"pip\s+install|rm\s|del\s|move\s|copy\s|docker\s+(?:compose\s+)?(?:up|restart))\b"
)
_CORRECTIONS: list[tuple[str, re.Pattern[str]]] = [
    ("research_before_editing", re.compile(r"(?i)(actual code|research first|inspect|read .* first|check .* path)")),
    ("verification_missing", re.compile(r"(?i)(didn['’]?t test|test it|still doesn['’]?t work|verify it)")),
    ("instruction_ignored", re.compile(r"(?i)(i told you|you forgot|ignored|don['’]?t do that)")),
    ("scope_misunderstood", re.compile(r"(?i)(not what i (?:asked|meant)|misunderstood|that['’]?s wrong)")),
    ("overengineering", re.compile(r"(?i)(overcomplicat|too complex|simpler)")),
    ("premature_stop", re.compile(r"(?i)(continue|don['’]?t stop|finish it)")),
]
_GENERIC_CORRECTION = re.compile(
    r"(?i)^(?:no[,.:!\s]|stop\b|wrong\b)|\bwhy did you\b|\buse .+ instead\b"
)


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


@dataclass(frozen=True, slots=True)
class ParsedTranscript:
    session: SessionRecord
    turns: tuple[TurnRecord, ...]
    actions: tuple[ActionRecord, ...]
    feedback: tuple[FeedbackRecord, ...]
    cursor_offset: int


def iter_complete_json(path: Path) -> Iterator[tuple[dict[str, Any], int, int]]:
    """Yield complete JSONL objects with byte offsets; ignore a partial tail."""
    offset = 0
    with path.open("rb") as handle:
        for raw in handle:
            start = offset
            offset += len(raw)
            if not raw.endswith(b"\n"):
                break
            try:
                value = json.loads(raw.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(value, dict):
                yield value, start, offset


def source_prefix_hash(path: Path, *, limit: int = 4096) -> str:
    with path.open("rb") as handle:
        return stable_hash(handle.read(limit).decode("latin1"))


def infer_scope(cwd: object) -> str:
    raw = str(cwd or "").strip()
    if not raw:
        return "global"
    name = Path(raw).name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    return f"project:{slug}" if slug else "global"


def infer_task_category(text: str) -> str:
    lowered = text.lower()
    categories = (
        ("debugging", ("bug", "debug", "fails", "broken", "error", "doesn't work")),
        ("testing", ("test", "coverage", "verify")),
        ("devops", ("deploy", "docker", "ci", "pipeline", "server")),
        ("ui", ("ui", "ux", "frontend", "screen", "visual", "css")),
        ("refactoring", ("refactor", "cleanup", "restructure")),
        ("architecture", ("architect", "design", "plan")),
        ("review", ("review", "audit")),
        ("research", ("research", "investigate", "compare")),
    )
    return next((name for name, words in categories if any(word in lowered for word in words)), "implementation")


def correction_theme(text: str) -> tuple[str, float] | None:
    for theme, pattern in _CORRECTIONS:
        if pattern.search(text):
            return theme, 0.9
    if _GENERIC_CORRECTION.search(text.strip()):
        return "redirection", 0.7
    return None


def action_kind(name: str, arguments: object = None) -> tuple[str, str]:
    normalized = name.rsplit("__", 1)[-1].lower()
    text = str(arguments or "")
    if normalized in _READ_TOOLS or any(token in normalized for token in ("read", "search", "find")):
        return "read", normalized
    if normalized in _MUTATION_TOOLS or any(token in normalized for token in ("edit", "write", "patch")):
        return "mutation", normalized
    if _VERIFICATION_WORDS.search(text):
        return "verification", command_family(text)
    if _MUTATION_COMMANDS.search(text):
        return "mutation", command_family(text)
    if normalized in {"bash", "exec_command", "shell", "command"}:
        return "command", command_family(text)
    return "tool", normalized


def command_family(value: object) -> str:
    text = str(value or "").lower()
    match = _VERIFICATION_WORDS.search(text)
    if match:
        return re.sub(r"\s+", " ", match.group(0)).strip()[:80]
    first = re.split(r"\s+", text.strip(), maxsplit=1)[0] if text.strip() else ""
    return first[:80]


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"text", "input_text", "output_text"}:
            parts.append(str(item.get("text") or ""))
    return "\n".join(filter(None, parts))


def _session_kind(path: Path, source: object = "") -> str:
    lowered = str(path).lower().replace("\\", "/")
    source_text = json.dumps(source, sort_keys=True).lower() if isinstance(source, dict) else str(source).lower()
    if "/subagents/" in lowered or "subagent" in source_text or "thread_spawn" in source_text:
        return "subagent"
    if any(word in source_text for word in ("automation", "cron", "scheduled")):
        return "automation"
    return "human"


def _turn_id(session_id: str, ordinal: int, start: int) -> str:
    return "turn-" + stable_hash(f"{session_id}:{ordinal}:{start}")[:20]


def _action_id(session_id: str, ordinal: int, start: int) -> str:
    return "action-" + stable_hash(f"{session_id}:{ordinal}:{start}")[:20]


def _feedback_for_turn(turn: TurnRecord) -> FeedbackRecord | None:
    if turn.role != "user" or turn.is_a2a or turn.ordinal == 1:
        return None
    matched = correction_theme(turn.excerpt)
    if matched is None:
        return None
    theme, confidence = matched
    return FeedbackRecord(
        "feedback-" + stable_hash(turn.turn_id + theme)[:20], turn.session_id,
        turn.turn_id, "correction", theme, turn.excerpt, confidence, True,
    )


def parse_claude(path: Path) -> ParsedTranscript | None:
    rows = list(iter_complete_json(path))
    if not rows:
        return None
    first = next((entry for entry, _, _ in rows if entry.get("sessionId") or entry.get("session_id")), rows[0][0])
    external_id = str(first.get("sessionId") or first.get("session_id") or path.stem)
    session_id = stable_hash(f"claude:{external_id}")
    kind = _session_kind(path, first.get("source"))
    turns: list[TurnRecord] = []
    action_rows: list[dict[str, Any]] = []
    pending: dict[str, int] = {}
    action_ordinal = 0
    for entry, start, end in rows:
        message = entry.get("message") if isinstance(entry.get("message"), dict) else entry
        role = str(message.get("role") or entry.get("type") or "")
        if role in {"user", "assistant"}:
            text = public_excerpt(_message_text(message.get("content")))
            if text:
                turns.append(TurnRecord(
                    _turn_id(session_id, len(turns) + 1, start), session_id,
                    len(turns) + 1, role, text, str(entry.get("timestamp") or ""), start, end,
                    text.startswith("[A2A from "),
                ))
        content = message.get("content") if isinstance(message, dict) else None
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use":
                action_ordinal += 1
                name = str(item.get("name") or "tool")
                kind_name, family = action_kind(name, item.get("input"))
                call_id = str(item.get("id") or action_ordinal)
                pending[call_id] = len(action_rows)
                action_rows.append({
                    "action_id": _action_id(session_id, action_ordinal, start),
                    "session_id": session_id,
                    "turn_id": turns[-1].turn_id if turns else "",
                    "ordinal": action_ordinal,
                    "kind": kind_name,
                    "name": name,
                    "status": "unknown",
                    "command_family": family,
                    "byte_start": start,
                    "byte_end": end,
                })
            elif item.get("type") == "tool_result":
                call_id = str(item.get("tool_use_id") or "")
                if call_id in pending:
                    failed = bool(item.get("is_error")) or re.search(
                        r"(?i)\b(error|failed|exit code [1-9])\b", str(item.get("content") or "")
                    )
                    action_rows[pending[call_id]]["status"] = "failed" if failed else "success"
                    action_rows[pending[call_id]]["byte_end"] = end
    feedback = tuple(item for turn in turns if (item := _feedback_for_turn(turn)))
    initial = next((turn.excerpt for turn in turns if turn.role == "user" and not turn.is_a2a), "")
    cwd = first.get("cwd") or next((entry.get("cwd") for entry, _, _ in rows if entry.get("cwd")), "")
    session = SessionRecord(
        session_id, external_id, "claude", kind, infer_scope(cwd),
        stable_hash(str(first.get("parentSessionId") or external_id)),
        stable_hash(str(first.get("parentSessionId") or "")) if first.get("parentSessionId") else "",
        str(first.get("model") or ""), "", str(cwd or ""),
        str(rows[0][0].get("timestamp") or ""), str(rows[-1][0].get("timestamp") or ""),
        initial, infer_task_category(initial), True,
    )
    actions = tuple(ActionRecord(**row) for row in action_rows)
    return ParsedTranscript(session, tuple(turns), actions, feedback, rows[-1][2])


def _codex_meta(rows: list[tuple[dict[str, Any], int, int]], path: Path) -> dict[str, Any]:
    for entry, _, _ in rows:
        if entry.get("type") == "session_meta" and isinstance(entry.get("payload"), dict):
            return dict(entry["payload"])
    return {"id": path.stem}


def _codex_status(output: object) -> str:
    text = str(output or "").lower()
    if re.search(r"(?:exit(?:_code)?[=: ]+0|process exited with code 0)", text):
        return "success"
    if re.search(r"(?:exit(?:_code)?[=: ]+[1-9]|error|failed)", text):
        return "failed"
    return "unknown"


def parse_codex(path: Path) -> ParsedTranscript | None:
    rows = list(iter_complete_json(path))
    if not rows:
        return None
    meta = _codex_meta(rows, path)
    external_id = str(meta.get("id") or meta.get("session_id") or path.stem)
    session_id = stable_hash(f"codex:{external_id}")
    kind = _session_kind(path, meta.get("source"))
    turns: list[TurnRecord] = []
    pending: dict[str, int] = {}
    actions: list[dict[str, Any]] = []
    for entry, start, end in rows:
        payload = entry.get("payload")
        if entry.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        payload_type = str(payload.get("type") or "")
        if payload_type == "message":
            role = str(payload.get("role") or "")
            text = public_excerpt(_message_text(payload.get("content")))
            if role in {"user", "assistant"} and text:
                turns.append(TurnRecord(
                    _turn_id(session_id, len(turns) + 1, start), session_id,
                    len(turns) + 1, role, text, str(entry.get("timestamp") or ""), start, end,
                    text.startswith("[A2A from "),
                ))
        elif payload_type in {"function_call", "custom_tool_call"}:
            name = str(payload.get("name") or payload.get("tool_name") or "tool")
            arguments = payload.get("arguments") or payload.get("input") or ""
            kind_name, family = action_kind(name, arguments)
            index = len(actions)
            call_id = str(payload.get("call_id") or payload.get("id") or index)
            pending[call_id] = index
            actions.append({
                "action_id": _action_id(session_id, index + 1, start),
                "session_id": session_id,
                "turn_id": turns[-1].turn_id if turns else "",
                "ordinal": index + 1,
                "kind": kind_name,
                "name": name,
                "status": "unknown",
                "command_family": family,
                "byte_start": start,
                "byte_end": end,
            })
        elif payload_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            if call_id in pending:
                actions[pending[call_id]]["status"] = _codex_status(payload.get("output"))
                actions[pending[call_id]]["byte_end"] = end
    normalized_actions = tuple(ActionRecord(**row) for row in actions)
    feedback = tuple(item for turn in turns if (item := _feedback_for_turn(turn)))
    initial = next((turn.excerpt for turn in turns if turn.role == "user" and not turn.is_a2a), "")
    cwd = meta.get("cwd") or ""
    parent = meta.get("parent_thread_id") or meta.get("parent_session_id") or ""
    session = SessionRecord(
        session_id, external_id, "codex", kind, infer_scope(cwd),
        stable_hash(str(parent or external_id)), stable_hash(str(parent)) if parent else "",
        str(meta.get("model") or meta.get("model_provider") or ""),
        str(meta.get("branch") or ""), str(cwd), str(rows[0][0].get("timestamp") or ""),
        str(rows[-1][0].get("timestamp") or ""), initial, infer_task_category(initial), True,
    )
    return ParsedTranscript(session, tuple(turns), normalized_actions, feedback, rows[-1][2])


def transcript_metadata(path: Path, provider: str) -> SessionRecord | None:
    """Read only the bounded transcript prefix needed for the source census."""
    rows: list[tuple[dict[str, Any], int, int]] = []
    for row in iter_complete_json(path):
        rows.append(row)
        if len(rows) >= 16:
            break
    if not rows:
        return None
    if provider == "codex":
        meta = _codex_meta(rows, path)
        external = str(meta.get("id") or meta.get("session_id") or path.stem)
        initial = ""
        for entry, _, _ in rows:
            payload = entry.get("payload")
            if entry.get("type") == "response_item" and isinstance(payload, dict) and payload.get("role") == "user":
                initial = public_excerpt(_message_text(payload.get("content")))
                if initial:
                    break
        parent = str(meta.get("parent_thread_id") or meta.get("parent_session_id") or "")
        cwd = meta.get("cwd") or ""
        return SessionRecord(
            stable_hash(f"codex:{external}"), external, "codex", _session_kind(path, meta.get("source")),
            infer_scope(cwd), stable_hash(parent or external), stable_hash(parent) if parent else "",
            str(meta.get("model") or meta.get("model_provider") or ""), str(meta.get("branch") or ""),
            str(cwd), str(rows[0][0].get("timestamp") or ""), "", initial,
            infer_task_category(initial), False,
        )
    first = next((entry for entry, _, _ in rows if entry.get("sessionId") or entry.get("session_id")), rows[0][0])
    external = str(first.get("sessionId") or first.get("session_id") or path.stem)
    initial = ""
    for entry, _, _ in rows:
        message = entry.get("message")
        if isinstance(message, dict) and message.get("role") == "user":
            initial = public_excerpt(_message_text(message.get("content")))
            if initial:
                break
    parent = str(first.get("parentSessionId") or "")
    cwd = first.get("cwd") or ""
    return SessionRecord(
        stable_hash(f"claude:{external}"), external, "claude", _session_kind(path, first.get("source")),
        infer_scope(cwd), stable_hash(parent or external), stable_hash(parent) if parent else "",
        str(first.get("model") or ""), "", str(cwd), str(rows[0][0].get("timestamp") or ""),
        "", initial, infer_task_category(initial), False,
    )


def parse_history_metadata(path: Path, provider: str) -> list[SessionRecord]:
    sessions: dict[str, SessionRecord] = {}
    for entry, _, _ in iter_complete_json(path):
        external_id = str(entry.get("session_id") or entry.get("sessionId") or "")
        if not external_id:
            continue
        session_id = stable_hash(f"{provider}:{external_id}")
        initial = public_excerpt(entry.get("text") or entry.get("display") or "")
        sessions[session_id] = SessionRecord(
            session_id, external_id, provider, "human", "global",
            stable_hash(external_id), "", "", "", "", str(entry.get("timestamp") or entry.get("ts") or ""),
            "", initial, infer_task_category(initial), False,
        )
    return list(sessions.values())


def parse_wezbridge_metadata(path: Path) -> list[SessionRecord]:
    sessions: dict[str, SessionRecord] = {}
    for entry, _, _ in iter_complete_json(path):
        corr = str(entry.get("corr") or entry.get("correlation_id") or "")
        if not corr:
            continue
        session_id = stable_hash(f"wezbridge:{corr}")
        project = str(entry.get("project") or entry.get("cwd") or "")
        sessions[session_id] = SessionRecord(
            session_id, corr, "wezbridge", "automation", infer_scope(project),
            stable_hash(corr), "", "", "", project,
            str(entry.get("timestamp") or entry.get("ts") or ""), "", "", "orchestration", False,
        )
    return list(sessions.values())


def wezbridge_statuses(path: Path) -> dict[str, set[str]]:
    statuses: dict[str, set[str]] = {}
    for entry, _, _ in iter_complete_json(path):
        corr = str(entry.get("corr") or entry.get("correlation_id") or "")
        if not corr:
            continue
        bucket = statuses.setdefault(corr, set())
        message_type = str(entry.get("type") or "").lower()
        event = str(entry.get("event") or "").lower()
        if message_type in {"request", "ack", "progress", "result", "error"}:
            bucket.add(message_type)
        if event == "a2a.thread-closed":
            bucket.add("closed")
        delivered = str(entry.get("delivered") or "").lower()
        if delivered == "ok":
            bucket.add("delivered")
    return statuses


__all__ = [
    "ParsedTranscript", "action_kind", "correction_theme", "infer_scope",
    "infer_task_category", "iter_complete_json", "parse_claude", "parse_codex",
    "parse_history_metadata", "parse_wezbridge_metadata", "source_prefix_hash",
    "stable_hash", "transcript_metadata", "wezbridge_statuses",
]
