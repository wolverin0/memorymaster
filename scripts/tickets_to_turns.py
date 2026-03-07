from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

# Supported local export shapes:
# 1) JSON array of ticket objects
# 2) JSON object with "tickets": [...]
# 3) JSONL where each line is a ticket object
#
# Output shape is operator inbox JSONL:
# {"session_id","thread_id","turn_id","user_text","assistant_text","observations","timestamp"}

_TIMESTAMP_KEYS = (
    "updated_at",
    "updatedAt",
    "created_at",
    "createdAt",
    "timestamp",
    "time",
    "ts",
    "date",
)
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


def _person_text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("display_name", "name", "login", "email", "id"):
            text = _to_str(value.get(key)).strip()
            if text:
                return text
        return ""
    return _to_str(value).strip()


def _labels(value: Any) -> list[str]:
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            text = _to_str(item).strip()
            if text:
                out.append(text)
        return out
    text = _to_str(value).strip()
    return [text] if text else []


def _comments(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            author = _person_text(item.get("author"))
            text = _first_non_empty(item, "body", "text", "message", "content")
            text = text.strip()
            if not text:
                continue
            if author:
                out.append(f"{author}: {text}")
            else:
                out.append(text)
            continue
        text = _to_str(item).strip()
        if text:
            out.append(text)
    return out


def _stable_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ticket_key(row: dict[str, Any]) -> str:
    return _first_non_empty(row, "key", "ticket_id", "ticket", "id", "number")


def _turn_id_for_ticket(row: dict[str, Any]) -> str:
    key = _ticket_key(row)
    if key:
        return f"ticket-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}"
    identity_payload = {
        "title": _first_non_empty(row, "title", "summary", "subject"),
        "description": _first_non_empty(row, "description", "body", "text"),
        "status": _first_non_empty(row, "status", "state"),
        "timestamp": _extract_timestamp(row),
    }
    return f"ticket-{_stable_digest(identity_payload)[:16]}"


def _rows_from_parsed(parsed: Any) -> list[dict[str, Any]]:
    if isinstance(parsed, list):
        rows: list[dict[str, Any]] = []
        for idx, item in enumerate(parsed, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"JSON array item {idx} must be an object")
            rows.append(item)
        return rows

    if isinstance(parsed, dict):
        tickets = parsed.get("tickets")
        if isinstance(tickets, list):
            rows: list[dict[str, Any]] = []
            for idx, item in enumerate(tickets, start=1):
                if not isinstance(item, dict):
                    raise ValueError(f"tickets[{idx}] must be an object")
                rows.append(item)
            return rows
        return [parsed]

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
            rows.append(item)
        return rows, len(rows)

    rows = _rows_from_parsed(parsed)
    return rows, len(rows)


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
    key = _ticket_key(row)
    if key:
        return key
    return _turn_id_for_ticket(row)


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
        key = _ticket_key(row)
        title = _first_non_empty(row, "title", "summary", "subject")
        description = _first_non_empty(row, "description", "body", "text")
        text_parts: list[str] = []
        if title:
            text_parts.append(title)
        if description and description != title:
            text_parts.append(description)
        user_text = "\n\n".join(part.strip() for part in text_parts if part.strip()).strip()
        if not user_text:
            user_text = json.dumps(row, ensure_ascii=True, sort_keys=True)

        observations: list[str] = []
        if key:
            observations.append(f"ticket={key}")
        status = _first_non_empty(row, "status", "state")
        if status:
            observations.append(f"status={status}")
        priority = _first_non_empty(row, "priority", "severity")
        if priority:
            observations.append(f"priority={priority}")
        assignee = _person_text(row.get("assignee"))
        if assignee:
            observations.append(f"assignee={assignee}")
        reporter = _person_text(row.get("reporter"))
        if reporter:
            observations.append(f"reporter={reporter}")
        labels = _labels(row.get("labels"))
        if labels:
            observations.append("labels=" + ",".join(labels))
        for comment in _comments(row.get("comments")):
            observations.append(f"comment={comment}")

        session_id = _first_non_empty(row, "session_id") or default_session_id
        project_key = _first_non_empty(row, "project", "board", "queue")
        thread_id = _first_non_empty(row, "thread_id") or project_key or default_thread_id
        turn_id = _first_non_empty(row, "turn_id") or _turn_id_for_ticket(row)

        converted.append(
            {
                "session_id": session_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "user_text": user_text,
                "assistant_text": "",
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
    parser = argparse.ArgumentParser(description="Convert local ticket export JSON/JSONL to operator inbox turns.")
    parser.add_argument("--input", required=True, help="Path to ticket export file")
    parser.add_argument("--output", required=True, help="Path to output JSONL")
    parser.add_argument("--session-id", default="tickets", help="Default session_id")
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
        default_session_id=_to_str(args.session_id).strip() or "tickets",
        default_thread_id=thread_id,
    )
    write_jsonl(output_path, turns)
    if cursor_path:
        _write_cursor(cursor_path, next_cursor)
    print(f"input_rows={input_rows} output_turns={len(turns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
