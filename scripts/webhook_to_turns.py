from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

_TIMESTAMP_KEYS = (
    "timestamp",
    "received_at",
    "created_at",
    "updated_at",
    "time",
    "ts",
)

_ASSISTANT_ROLES = {"assistant", "ai", "bot", "model", "agent"}
_JSONL_SUFFIXES = {".jsonl", ".ndjson"}


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _first_non_empty(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            continue
        text = _to_str(value).strip()
        if text:
            return text
    return ""


def _extract_timestamp(payload: dict[str, Any]) -> str:
    for key in _TIMESTAMP_KEYS:
        text = _to_str(payload.get(key)).strip()
        if text:
            return text
    return ""


def _content_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            text = _content_to_text(item).strip()
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(value, dict):
        for key in ("text", "body", "message", "content", "title"):
            text = _content_to_text(value.get(key)).strip()
            if text:
                return text
        return ""
    return _to_str(value)


def _normalize_observations(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            text = _to_str(item).strip()
            if text:
                out.append(text)
        return out
    text = _to_str(value).strip()
    return [text] if text else []


def _jsonl_rows(path: Path, *, start_line: int) -> tuple[list[dict[str, Any]], int, int]:
    rows: list[dict[str, Any]] = []
    raw_rows = 0
    total_lines = 0
    for index, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        payload = line.strip()
        if not payload:
            total_lines = index
            continue
        total_lines = index
        if index <= start_line:
            continue
        item = json.loads(payload)
        if not isinstance(item, dict):
            raise ValueError(f"JSONL line {index} in {path} must be an object")
        raw_rows += 1
        rows.extend(_expand_row(item))
    return rows, raw_rows, total_lines


def _json_rows(path: Path) -> tuple[list[dict[str, Any]], int]:
    raw_text = path.read_text(encoding="utf-8-sig")
    stripped = raw_text.strip()
    if not stripped:
        return [], 0
    payload = json.loads(stripped)
    rows = _rows_from_parsed(payload)
    return rows, 1


def _expand_row(item: dict[str, Any]) -> list[dict[str, Any]]:
    events = item.get("events")
    if isinstance(events, list):
        expanded: list[dict[str, Any]] = []
        for nested in events:
            if not isinstance(nested, dict):
                continue
            merged = dict(nested)
            for inherit_key in ("session_id", "thread_id", "source", "provider"):
                if inherit_key not in merged and item.get(inherit_key) is not None:
                    merged[inherit_key] = item.get(inherit_key)
            expanded.append(merged)
        if expanded:
            return expanded
    return [item]


def _rows_from_parsed(parsed: Any) -> list[dict[str, Any]]:
    if isinstance(parsed, list):
        rows: list[dict[str, Any]] = []
        for idx, item in enumerate(parsed, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"JSON array item {idx} must be an object")
            rows.extend(_expand_row(item))
        return rows
    if isinstance(parsed, dict):
        return _expand_row(parsed)
    raise ValueError("Input must be JSON object, JSON array, or JSONL objects")


def _candidate_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(f"Input path not found: {path}")
    files: list[Path] = []
    for file in path.rglob("*"):
        if not file.is_file():
            continue
        suffix = file.suffix.lower()
        if suffix in {".json", ".jsonl", ".ndjson"}:
            files.append(file)
    files.sort(key=lambda value: value.as_posix().lower())
    return files


def load_rows(
    path: Path,
    *,
    cursor: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    files = _candidate_files(path)
    root = path if path.is_dir() else path.parent
    cursor_payload = cursor if isinstance(cursor, dict) else {}
    last_file = _to_str(cursor_payload.get("last_file")).strip()
    last_line = int(cursor_payload.get("last_line") or 0)
    rows: list[dict[str, Any]] = []
    raw_rows = 0
    next_last_file = last_file
    next_last_line = last_line

    for file in files:
        rel = file.name if path.is_file() else file.relative_to(root).as_posix()
        if last_file and rel < last_file:
            continue

        suffix = file.suffix.lower()
        start_line = 0
        if rel == last_file:
            if suffix in _JSONL_SUFFIXES:
                start_line = max(0, last_line)
            elif last_line >= 1:
                continue

        if suffix in _JSONL_SUFFIXES:
            file_rows, file_raw_rows, total_lines = _jsonl_rows(file, start_line=start_line)
            rows.extend(file_rows)
            raw_rows += file_raw_rows
            next_last_file = rel
            next_last_line = total_lines
            continue

        file_rows, file_raw_rows = _json_rows(file)
        rows.extend(file_rows)
        raw_rows += file_raw_rows
        next_last_file = rel
        next_last_line = 1

    next_cursor = {"version": 1, "last_file": next_last_file, "last_line": next_last_line}
    return rows, raw_rows, next_cursor


def _stable_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _event_payload(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("payload", "body", "data"):
        value = row.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _headers(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("headers")
    if isinstance(value, dict):
        return value
    return {}


def _event_type(row: dict[str, Any], payload: dict[str, Any]) -> str:
    headers = _headers(row)
    return (
        _first_non_empty(row, "event_type", "event", "type")
        or _to_str(headers.get("x-github-event")).strip()
        or _first_non_empty(payload, "event_type", "type")
    )


def _delivery_id(row: dict[str, Any]) -> str:
    headers = _headers(row)
    return _first_non_empty(row, "delivery_id", "event_id", "id") or _to_str(headers.get("x-github-delivery")).strip()


def _repository(row: dict[str, Any], payload: dict[str, Any]) -> str:
    direct = _first_non_empty(row, "repository", "repo", "project")
    if direct:
        return direct
    repo_payload = payload.get("repository")
    if isinstance(repo_payload, dict):
        return _first_non_empty(repo_payload, "full_name", "name")
    return ""


def _thread_id(row: dict[str, Any], payload: dict[str, Any], default_thread_id: str) -> str:
    direct = _first_non_empty(row, "thread_id", "thread", "conversation_id", "channel")
    if direct:
        return direct
    for key in ("issue", "pull_request"):
        value = payload.get(key)
        if isinstance(value, dict):
            number = _to_str(value.get("number")).strip()
            if number:
                prefix = "issue" if key == "issue" else "pr"
                return f"{prefix}-{number}"
    discussion = payload.get("discussion")
    if isinstance(discussion, dict):
        number = _to_str(discussion.get("number")).strip()
        if number:
            return f"discussion-{number}"
    return default_thread_id


def _summary_text(row: dict[str, Any], payload: dict[str, Any], event_type: str, action: str) -> str:
    explicit = _content_to_text(row.get("user_text")).strip()
    if explicit:
        return explicit

    title = (
        _first_non_empty(row, "title", "subject", "summary")
        or _first_non_empty(payload, "title", "subject")
    )
    issue = payload.get("issue")
    if isinstance(issue, dict):
        title = title or _first_non_empty(issue, "title")
    pr = payload.get("pull_request")
    if isinstance(pr, dict):
        title = title or _first_non_empty(pr, "title")

    body = (
        _content_to_text(row.get("body") or row.get("text") or row.get("message")).strip()
        or _content_to_text(payload.get("body") or payload.get("text") or payload.get("message")).strip()
    )
    comment = payload.get("comment")
    if isinstance(comment, dict):
        comment_text = _content_to_text(comment.get("body")).strip()
        if comment_text:
            body = body or comment_text

    event_line = event_type.strip()
    if action.strip():
        event_line = f"{event_line}.{action.strip()}" if event_line else action.strip()
    if not event_line:
        event_line = "webhook_event"

    parts: list[str] = [event_line]
    if title.strip():
        parts.append(title.strip())
    if body.strip():
        parts.append(body.strip())
    return "\n\n".join(parts)


def _turn_id(row: dict[str, Any], payload: dict[str, Any], event_type: str, action: str, thread_id: str, timestamp: str) -> str:
    explicit = _first_non_empty(row, "turn_id")
    if explicit:
        return explicit
    identity = _delivery_id(row)
    if identity:
        return f"webhook-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"
    digest_payload = {
        "event_type": event_type,
        "action": action,
        "thread_id": thread_id,
        "timestamp": timestamp,
        "repository": _repository(row, payload),
        "payload_excerpt": _content_to_text(payload.get("title") or payload.get("body") or payload.get("text"))[:240],
    }
    return f"webhook-{_stable_digest(digest_payload)[:16]}"


def convert_rows(
    rows: list[dict[str, Any]],
    *,
    default_session_id: str,
    default_thread_id: str,
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for row in rows:
        payload = _event_payload(row)
        event_type = _event_type(row, payload)
        action = _first_non_empty(row, "action") or _first_non_empty(payload, "action")
        timestamp = _extract_timestamp(row) or _extract_timestamp(payload)
        thread_id = _thread_id(row, payload, default_thread_id)
        text = _summary_text(row, payload, event_type, action)

        role = _first_non_empty(row, "role", "sender_role").lower()
        assistant_text = text if role in _ASSISTANT_ROLES else _content_to_text(row.get("assistant_text")).strip()
        user_text = _content_to_text(row.get("user_text")).strip()
        if assistant_text:
            if not user_text:
                user_text = ""
        else:
            user_text = user_text or text

        source = _first_non_empty(row, "source", "provider")
        repository = _repository(row, payload)
        actor = _first_non_empty(row, "actor", "sender", "user")
        if not actor:
            sender = payload.get("sender")
            if isinstance(sender, dict):
                actor = _first_non_empty(sender, "login", "name", "id")

        observations: list[str] = []
        if source:
            observations.append(f"source={source}")
        if event_type:
            observations.append(f"event={event_type}")
        if action:
            observations.append(f"action={action}")
        delivery_id = _delivery_id(row)
        if delivery_id:
            observations.append(f"delivery_id={delivery_id}")
        if repository:
            observations.append(f"repository={repository}")
        if actor:
            observations.append(f"actor={actor}")
        observations.extend(_normalize_observations(row.get("observations")))

        session_id = _first_non_empty(row, "session_id") or default_session_id
        turn_id = _turn_id(row, payload, event_type, action, thread_id, timestamp)
        converted.append(
            {
                "session_id": session_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "user_text": user_text,
                "assistant_text": assistant_text,
                "observations": observations,
                "timestamp": timestamp,
            }
        )
    return converted


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def _read_cursor(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_cursor(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bridge webhook JSON/JSONL files into normalized operator turns.")
    parser.add_argument("--input", required=True, help="Input JSON/JSONL file or directory")
    parser.add_argument("--output", required=True, help="Output JSONL path")
    parser.add_argument("--session-id", default="webhook", help="Default session_id")
    parser.add_argument("--thread-id", default="webhook-main", help="Default thread_id")
    parser.add_argument(
        "--cursor-json",
        default=None,
        help="Optional cursor JSON for incremental file tailing",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"Input path not found: {input_path}")

    cursor_path = Path(args.cursor_json) if args.cursor_json else None
    cursor = _read_cursor(cursor_path) if cursor_path else {}

    rows, raw_rows, next_cursor = load_rows(input_path, cursor=cursor)
    turns = convert_rows(
        rows,
        default_session_id=_to_str(args.session_id).strip() or "webhook",
        default_thread_id=_to_str(args.thread_id).strip() or "webhook-main",
    )
    write_jsonl(Path(args.output), turns)
    if cursor_path:
        _write_cursor(cursor_path, next_cursor)
    print(f"input_rows={raw_rows} output_turns={len(turns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
