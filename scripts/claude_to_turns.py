from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


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


def _is_message_like(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if "role" not in item:
        return False
    return "content" in item or "text" in item


def _extract_timestamp(payload: dict[str, Any]) -> str:
    for key in ("timestamp", "created_at", "createdAt", "time", "ts"):
        text = _to_str(payload.get(key)).strip()
        if text:
            return text
    return ""


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (int, float, bool)):
        return str(content)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                block_type = _to_str(block.get("type")).strip().lower()
                if block_type == "text" and block.get("text") is not None:
                    text = _to_str(block.get("text")).strip()
                    if text:
                        parts.append(text)
                    continue
                if "text" in block:
                    text = _to_str(block.get("text")).strip()
                    if text:
                        parts.append(text)
                    continue
                if "content" in block:
                    text = _content_to_text(block.get("content")).strip()
                    if text:
                        parts.append(text)
                    continue
                continue
            text = _to_str(block).strip()
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict):
        if "text" in content:
            return _content_to_text(content.get("text"))
        if "content" in content:
            return _content_to_text(content.get("content"))
        return ""
    return _to_str(content)


def _jsonish_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    except TypeError:
        return _to_str(value)


def _entry_text(entry: dict[str, Any], *, for_observation: bool) -> str:
    text = ""
    if "text" in entry:
        text = _content_to_text(entry.get("text")).strip()
    if not text and "content" in entry:
        text = _content_to_text(entry.get("content")).strip()
    if text:
        return text
    if for_observation:
        if "content" in entry:
            return _jsonish_to_text(entry.get("content")).strip()
        if "text" in entry:
            return _jsonish_to_text(entry.get("text")).strip()
        return _jsonish_to_text(entry).strip()
    return ""


def _canonical_items(items: list[Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = _to_str(item.get("role")).strip().lower()
        if not role:
            continue
        text = _entry_text(item, for_observation=role not in {"user", "assistant"}).strip()
        if not text:
            continue
        out.append(
            {
                "role": role,
                "text": text,
                "timestamp": _extract_timestamp(item),
            }
        )
    return out


def _pair_items_to_turns(items: list[dict[str, str]], *, row_timestamp: str) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    pending_observations: list[str] = []
    current: dict[str, Any] | None = None

    for item in items:
        role = item["role"]
        text = item["text"].strip()
        timestamp = item.get("timestamp", "").strip()
        if not text:
            continue

        if role == "user":
            if current is None:
                current = {
                    "user_parts": [],
                    "assistant_parts": [],
                    "observations": list(pending_observations),
                    "timestamp": timestamp,
                }
                pending_observations.clear()
            elif current["assistant_parts"]:
                turns.append(
                    {
                        "user_text": "\n".join(current["user_parts"]).strip(),
                        "assistant_text": "\n".join(current["assistant_parts"]).strip(),
                        "observations": list(current["observations"]),
                        "timestamp": current["timestamp"] or row_timestamp,
                    }
                )
                current = {
                    "user_parts": [],
                    "assistant_parts": [],
                    "observations": list(pending_observations),
                    "timestamp": timestamp,
                }
                pending_observations.clear()
            current["user_parts"].append(text)
            if not current["timestamp"]:
                current["timestamp"] = timestamp
            continue

        if role == "assistant":
            if current is None:
                current = {
                    "user_parts": [],
                    "assistant_parts": [],
                    "observations": list(pending_observations),
                    "timestamp": timestamp,
                }
                pending_observations.clear()
            current["assistant_parts"].append(text)
            if not current["timestamp"]:
                current["timestamp"] = timestamp
            continue

        if current is not None and not current["assistant_parts"]:
            current["observations"].append(text)
            if not current["timestamp"]:
                current["timestamp"] = timestamp
        else:
            pending_observations.append(text)

    if current is not None:
        turns.append(
            {
                "user_text": "\n".join(current["user_parts"]).strip(),
                "assistant_text": "\n".join(current["assistant_parts"]).strip(),
                "observations": list(current["observations"]),
                "timestamp": current["timestamp"] or row_timestamp,
            }
        )

    if pending_observations:
        if turns:
            turns[-1]["observations"].extend(pending_observations)
        else:
            turns.append(
                {
                    "user_text": "",
                    "assistant_text": "",
                    "observations": list(pending_observations),
                    "timestamp": row_timestamp,
                }
            )
    return turns


def _normalize_row_count(row: dict[str, Any]) -> int:
    if isinstance(row.get("messages"), list):
        return len(row["messages"])
    if isinstance(row.get("events"), list):
        return len(row["events"])
    if _is_message_like(row):
        return 1
    return 1


def _load_rows(path: Path) -> tuple[list[dict[str, Any]], int, int]:
    raw_text = path.read_text(encoding="utf-8-sig")
    stripped = raw_text.strip()
    if not stripped:
        return [], 0, 0

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        rows: list[dict[str, Any]] = []
        input_rows = 0
        input_messages = 0
        for idx, line in enumerate(raw_text.splitlines(), start=1):
            payload = line.strip()
            if not payload:
                continue
            try:
                item = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at line {idx}: {exc.msg}") from exc
            if _is_message_like(item):
                row = {"messages": [item]}
            elif isinstance(item, dict):
                row = item
            else:
                raise ValueError(f"JSONL line {idx} must be an object")
            rows.append(row)
            input_rows += 1
            input_messages += _normalize_row_count(row)
        return rows, input_rows, input_messages

    rows: list[dict[str, Any]] = []
    input_rows = 0
    input_messages = 0

    if isinstance(parsed, list):
        if all(_is_message_like(item) for item in parsed):
            rows = [{"messages": parsed}]
            input_rows = 1
            input_messages = len(parsed)
            return rows, input_rows, input_messages
        for idx, item in enumerate(parsed, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"JSON array item {idx} must be an object")
            rows.append(item)
            input_rows += 1
            input_messages += _normalize_row_count(item)
        return rows, input_rows, input_messages

    if isinstance(parsed, dict):
        if _is_message_like(parsed):
            row = {"messages": [parsed]}
        else:
            row = parsed
        rows = [row]
        input_rows = 1
        input_messages = _normalize_row_count(row)
        return rows, input_rows, input_messages

    raise ValueError("Input must be a JSON object, JSON array, or JSONL objects")


def _convert_rows(
    rows: list[dict[str, Any]],
    *,
    default_session_id: str,
    default_thread_id: str,
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    bridge_index = 0

    def next_turn_id() -> str:
        nonlocal bridge_index
        bridge_index += 1
        return f"bridge-{bridge_index:04d}"

    for row in rows:
        row_session = _to_str(row.get("session_id")).strip() or default_session_id
        row_thread = _to_str(row.get("thread_id")).strip() or default_thread_id
        row_turn_id = _to_str(row.get("turn_id")).strip()
        row_timestamp = _extract_timestamp(row)
        row_extra_observations = _normalize_observations(row.get("observations"))

        partial_turns: list[dict[str, Any]]
        if isinstance(row.get("messages"), list):
            partial_turns = _pair_items_to_turns(
                _canonical_items(row["messages"]),
                row_timestamp=row_timestamp,
            )
        elif isinstance(row.get("events"), list):
            partial_turns = _pair_items_to_turns(
                _canonical_items(row["events"]),
                row_timestamp=row_timestamp,
            )
        else:
            partial_turns = [
                {
                    "turn_id": row_turn_id,
                    "user_text": _to_str(row.get("user_text")).strip(),
                    "assistant_text": _to_str(row.get("assistant_text")).strip(),
                    "observations": _normalize_observations(row.get("observations")),
                    "timestamp": row_timestamp,
                }
            ]
            row_extra_observations = []

        if row_extra_observations:
            if partial_turns:
                partial_turns[0]["observations"] = _normalize_observations(partial_turns[0].get("observations"))
                partial_turns[0]["observations"] = row_extra_observations + partial_turns[0]["observations"]
            else:
                partial_turns = [
                    {
                        "user_text": "",
                        "assistant_text": "",
                        "observations": list(row_extra_observations),
                        "timestamp": row_timestamp,
                    }
                ]

        if row_turn_id and len(partial_turns) == 1 and not _to_str(partial_turns[0].get("turn_id")).strip():
            partial_turns[0]["turn_id"] = row_turn_id

        for turn in partial_turns:
            observations = _normalize_observations(turn.get("observations"))
            output_turn_id = _to_str(turn.get("turn_id")).strip() or next_turn_id()
            converted.append(
                {
                    "session_id": row_session,
                    "thread_id": row_thread,
                    "turn_id": output_turn_id,
                    "user_text": _to_str(turn.get("user_text")).strip(),
                    "assistant_text": _to_str(turn.get("assistant_text")).strip(),
                    "observations": observations,
                    "timestamp": _to_str(turn.get("timestamp")).strip() or row_timestamp,
                }
            )

    return converted


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert Claude exports to operator inbox JSONL turns.")
    parser.add_argument("--input", required=True, help="Path to JSON or JSONL export")
    parser.add_argument("--output", required=True, help="Path to output JSONL")
    parser.add_argument("--session-id", default="claude", help="Default session_id for output rows")
    parser.add_argument(
        "--thread-id",
        default=None,
        help="Default thread_id for output rows (defaults to input filename stem)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = Path(args.input)
    output_path = Path(args.output)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    thread_id = args.thread_id if args.thread_id is not None else input_path.stem
    rows, input_rows, input_messages = _load_rows(input_path)
    turns = _convert_rows(
        rows,
        default_session_id=_to_str(args.session_id).strip() or "claude",
        default_thread_id=_to_str(thread_id).strip() or input_path.stem,
    )
    _write_jsonl(output_path, turns)
    print(f"input_rows={input_rows} input_messages={input_messages} output_turns={len(turns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
