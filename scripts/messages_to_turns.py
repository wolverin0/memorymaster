from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

# Supported local export shapes:
# 1) JSON array of message/email objects
# 2) JSON object with "messages": [...], "emails": [...], or "threads": [...]
# 3) JSONL where each line is one message/email object or a wrapper object
#
# Output shape is operator inbox JSONL:
# {"session_id","thread_id","turn_id","user_text","assistant_text","observations","timestamp"}

_TIMESTAMP_KEYS = (
    "timestamp",
    "created_at",
    "createdAt",
    "sent_at",
    "sentAt",
    "date",
    "time",
    "ts",
)

_ASSISTANT_ROLES = {"assistant", "ai", "bot", "model", "agent"}
_CURSOR_ID_LIMIT = 500


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _extract_timestamp(payload: dict[str, Any]) -> str:
    for key in _TIMESTAMP_KEYS:
        text = _to_str(payload.get(key)).strip()
        if text:
            return text
    return ""


def _first_non_empty(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            continue
        text = _to_str(value).strip()
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
            if isinstance(item, dict):
                for key in ("text", "content", "body", "message", "value"):
                    text = _content_to_text(item.get(key)).strip()
                    if text:
                        parts.append(text)
                        break
                continue
            text = _to_str(item).strip()
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(value, dict):
        for key in ("text", "content", "body", "message", "value"):
            text = _content_to_text(value.get(key)).strip()
            if text:
                return text
        return ""
    return _to_str(value)


def _person_text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("display_name", "name", "username", "login", "email", "id"):
            text = _to_str(value.get(key)).strip()
            if text:
                return text
        return ""
    return _to_str(value).strip()


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            text = _person_text(item)
            if text:
                out.append(text)
        return out
    text = _person_text(value)
    return [text] if text else []


def _stable_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _turn_id_for_message(row: dict[str, Any]) -> str:
    identity = _first_non_empty(row, "message_id", "id", "ts", "event_id")
    if identity:
        return f"msg-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"
    identity_payload = {
        "thread": _first_non_empty(row, "thread_id", "thread_ts", "conversation_id", "channel"),
        "sender": _person_text(row.get("from")) or _person_text(row.get("sender")) or _person_text(row.get("user")),
        "subject": _first_non_empty(row, "subject"),
        "body": _content_to_text(row.get("body") or row.get("text") or row.get("content") or row.get("message")),
        "timestamp": _extract_timestamp(row),
    }
    return f"msg-{_stable_digest(identity_payload)[:16]}"


def _is_message_like(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if "subject" in item and ("body" in item or "text" in item or "content" in item):
        return True
    for key in ("body", "text", "content", "message"):
        if key in item:
            return True
    return False


def _expand_wrapper(item: dict[str, Any]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []

    for key in ("messages", "emails", "items", "events"):
        value = item.get(key)
        if isinstance(value, list):
            for nested in value:
                if not isinstance(nested, dict):
                    continue
                merged = dict(nested)
                for inherit_key in ("session_id", "thread_id", "channel", "conversation_id"):
                    inherit_value = item.get(inherit_key)
                    if inherit_value is not None and inherit_key not in merged:
                        merged[inherit_key] = inherit_value
                expanded.append(merged)
            if expanded:
                return expanded

    threads = item.get("threads")
    if isinstance(threads, list):
        for thread in threads:
            if not isinstance(thread, dict):
                continue
            thread_id = _first_non_empty(thread, "thread_id", "id", "thread_ts", "conversation_id")
            thread_channel = _first_non_empty(thread, "channel", "conversation_id")
            for key in ("messages", "emails", "items", "events"):
                nested_items = thread.get(key)
                if not isinstance(nested_items, list):
                    continue
                for nested in nested_items:
                    if not isinstance(nested, dict):
                        continue
                    merged = dict(nested)
                    if thread_id and "thread_id" not in merged:
                        merged["thread_id"] = thread_id
                    if thread_channel and "channel" not in merged:
                        merged["channel"] = thread_channel
                    for inherit_key in ("session_id",):
                        inherit_value = item.get(inherit_key)
                        if inherit_value is not None and inherit_key not in merged:
                            merged[inherit_key] = inherit_value
                    expanded.append(merged)
        if expanded:
            return expanded

    if _is_message_like(item):
        return [item]
    return []


def _rows_from_parsed(parsed: Any) -> list[dict[str, Any]]:
    if isinstance(parsed, list):
        rows: list[dict[str, Any]] = []
        for idx, item in enumerate(parsed, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"JSON array item {idx} must be an object")
            expanded = _expand_wrapper(item)
            if expanded:
                rows.extend(expanded)
            elif _is_message_like(item):
                rows.append(item)
        return rows

    if isinstance(parsed, dict):
        expanded = _expand_wrapper(parsed)
        if expanded:
            return expanded
        return [parsed] if _is_message_like(parsed) else []

    raise ValueError("Input must be a JSON object, JSON array, or JSONL objects")


def load_rows(path: Path) -> tuple[list[dict[str, Any]], int]:
    raw_text = path.read_text(encoding="utf-8-sig")
    stripped = raw_text.strip()
    if not stripped:
        return [], 0

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        rows: list[dict[str, Any]] = []
        input_rows = 0
        for idx, line in enumerate(raw_text.splitlines(), start=1):
            payload = line.strip()
            if not payload:
                continue
            try:
                item = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at line {idx}: {exc.msg}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"JSONL line {idx} must be an object")
            input_rows += 1
            rows.extend(_expand_wrapper(item) or ([item] if _is_message_like(item) else []))
        return rows, input_rows

    rows = _rows_from_parsed(parsed)
    input_rows = len(rows) if isinstance(parsed, list) else 1
    return rows, input_rows


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


def _cursor_id_for_row(row: dict[str, Any]) -> str:
    explicit = _first_non_empty(row, "message_id", "id", "event_id")
    if explicit:
        return explicit
    return _turn_id_for_message(row)


def _cursor_state(cursor: dict[str, Any] | None) -> tuple[str, set[str], set[str]]:
    if not isinstance(cursor, dict):
        return "", set(), set()
    latest_ts = _to_str(cursor.get("latest_ts")).strip()
    latest_ids_raw = cursor.get("latest_ids")
    latest_ids = (
        {_to_str(item).strip() for item in latest_ids_raw if _to_str(item).strip()}
        if isinstance(latest_ids_raw, list)
        else set()
    )
    no_ts_ids_raw = cursor.get("no_ts_recent_ids")
    no_ts_ids = (
        {_to_str(item).strip() for item in no_ts_ids_raw if _to_str(item).strip()}
        if isinstance(no_ts_ids_raw, list)
        else set()
    )
    return latest_ts, latest_ids, no_ts_ids


def load_rows_incremental(
    path: Path,
    *,
    cursor: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    rows, input_rows = load_rows(path)
    sorted_rows = sorted(rows, key=lambda row: (_extract_timestamp(row), _cursor_id_for_row(row)))
    latest_ts, latest_ids, no_ts_seen_ids = _cursor_state(cursor)
    next_latest_ts = latest_ts
    next_latest_ids = set(latest_ids)
    next_no_ts_ids = set(no_ts_seen_ids)

    incremental_rows: list[dict[str, Any]] = []
    for row in sorted_rows:
        row_ts = _extract_timestamp(row)
        row_id = _cursor_id_for_row(row)
        if row_ts:
            if latest_ts:
                if row_ts < latest_ts:
                    continue
                if row_ts == latest_ts and row_id and row_id in latest_ids:
                    continue
            incremental_rows.append(row)
            if not next_latest_ts or row_ts > next_latest_ts:
                next_latest_ts = row_ts
                next_latest_ids = {row_id} if row_id else set()
            elif row_ts == next_latest_ts and row_id:
                next_latest_ids.add(row_id)
            continue

        if row_id and row_id in no_ts_seen_ids:
            continue
        incremental_rows.append(row)
        if row_id:
            next_no_ts_ids.add(row_id)

    next_cursor = {
        "version": 1,
        "sort": "timestamp+id",
        "latest_ts": next_latest_ts,
        "latest_ids": sorted(next_latest_ids)[:_CURSOR_ID_LIMIT],
        "no_ts_recent_ids": sorted(next_no_ts_ids)[:_CURSOR_ID_LIMIT],
    }
    return incremental_rows, input_rows, next_cursor


def convert_rows(
    rows: list[dict[str, Any]],
    *,
    default_session_id: str,
    default_thread_id: str,
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for row in rows:
        subject = _first_non_empty(row, "subject")
        body = _content_to_text(row.get("body") or row.get("text") or row.get("content") or row.get("message")).strip()
        if subject and body and subject.strip() not in body:
            text = f"{subject.strip()}\n\n{body}"
        else:
            text = subject.strip() or body
        if not text:
            text = json.dumps(row, ensure_ascii=True, sort_keys=True)

        role = _first_non_empty(row, "role", "message_role", "sender_role").lower()
        assistant_text = text if role in _ASSISTANT_ROLES else ""
        user_text = "" if assistant_text else text

        sender = _person_text(row.get("from")) or _person_text(row.get("sender")) or _person_text(row.get("user"))
        recipients = _as_list(row.get("to"))
        cc = _as_list(row.get("cc"))
        channel = _first_non_empty(row, "channel", "conversation_id")

        observations: list[str] = []
        if sender:
            observations.append(f"from={sender}")
        if recipients:
            observations.append("to=" + ",".join(recipients))
        if cc:
            observations.append("cc=" + ",".join(cc))
        if channel:
            observations.append(f"channel={channel}")
        thread_ref = _first_non_empty(row, "thread_ts")
        if thread_ref:
            observations.append(f"thread_ts={thread_ref}")

        session_id = _first_non_empty(row, "session_id") or default_session_id
        thread_id = (
            _first_non_empty(row, "thread_id", "thread_ts", "conversation_id", "channel")
            or default_thread_id
        )
        turn_id = _first_non_empty(row, "turn_id") or _turn_id_for_message(row)

        converted.append(
            {
                "session_id": session_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "user_text": user_text,
                "assistant_text": assistant_text,
                "observations": observations,
                "timestamp": _extract_timestamp(row),
            }
        )
    return converted


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert local Slack/email-like JSON or JSONL exports to operator inbox turns."
    )
    parser.add_argument("--input", required=True, help="Path to messages export file")
    parser.add_argument("--output", required=True, help="Path to output JSONL")
    parser.add_argument("--session-id", default="messages", help="Default session_id")
    parser.add_argument(
        "--thread-id",
        default=None,
        help="Default thread_id (defaults to input filename stem)",
    )
    parser.add_argument(
        "--cursor-json",
        default=None,
        help="Optional cursor state JSON for incremental imports",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = Path(args.input)
    output_path = Path(args.output)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    thread_id = _to_str(args.thread_id).strip() or input_path.stem
    cursor_path = Path(args.cursor_json) if args.cursor_json else None
    if cursor_path:
        cursor = _read_cursor(cursor_path)
        rows, input_rows, next_cursor = load_rows_incremental(input_path, cursor=cursor)
    else:
        rows, input_rows = load_rows(input_path)
        next_cursor = {}
    turns = convert_rows(
        rows,
        default_session_id=_to_str(args.session_id).strip() or "messages",
        default_thread_id=thread_id,
    )
    write_jsonl(output_path, turns)
    if cursor_path:
        _write_cursor(cursor_path, next_cursor)
    print(f"input_rows={input_rows} output_turns={len(turns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
