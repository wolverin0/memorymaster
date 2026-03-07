from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

_USER_ROLES = {"user", "human", "customer", "prompt"}
_ASSISTANT_ROLES = {"assistant", "model", "ai", "bot", "chatgpt", "claude", "gemini"}
_TIMESTAMP_KEYS = (
    "timestamp",
    "created_at",
    "createdAt",
    "time",
    "ts",
    "create_time",
    "updated_at",
    "updatedAt",
    "date",
)


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


def _extract_timestamp(payload: dict[str, Any]) -> str:
    for key in _TIMESTAMP_KEYS:
        text = _to_str(payload.get(key)).strip()
        if text:
            return text
    return ""


def _normalize_role(role: str) -> str:
    normalized = role.strip().lower()
    if not normalized:
        return ""
    if normalized in _USER_ROLES:
        return "user"
    if normalized in _ASSISTANT_ROLES:
        return "assistant"
    return normalized


def _role_from_entry(entry: dict[str, Any]) -> str:
    role = _to_str(entry.get("role")).strip()
    if role:
        return role

    author = entry.get("author")
    if isinstance(author, dict):
        role = _to_str(author.get("role")).strip() or _to_str(author.get("name")).strip()
        if role:
            return role
    else:
        role = _to_str(author).strip()
        if role:
            return role

    for key in ("sender", "speaker", "from"):
        role = _to_str(entry.get(key)).strip()
        if role:
            return role

    if isinstance(entry.get("message"), dict):
        return _role_from_entry(entry["message"])
    return ""


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
                if block_type in {"text", "output_text"} and block.get("text") is not None:
                    text = _to_str(block.get("text")).strip()
                    if text:
                        parts.append(text)
                    continue
                for key in ("text", "content", "parts", "message", "value"):
                    if key not in block:
                        continue
                    text = _content_to_text(block.get(key)).strip()
                    if text:
                        parts.append(text)
                    break
                continue
            text = _to_str(block).strip()
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict):
        if content.get("content_type") == "text" and isinstance(content.get("parts"), list):
            return _content_to_text(content.get("parts"))
        for key in ("text", "content", "parts", "message", "output_text", "value", "response"):
            if key in content:
                return _content_to_text(content.get(key))
        return ""
    return _to_str(content)


def _entry_text(entry: dict[str, Any], *, for_observation: bool) -> str:
    for key in ("text", "content", "parts", "message"):
        if key not in entry:
            continue
        text = _content_to_text(entry.get(key)).strip()
        if text:
            return text
    if for_observation:
        return _jsonish_to_text(entry).strip()
    return ""


def _is_message_like(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if isinstance(item.get("message"), dict):
        return _is_message_like(item["message"])
    role = _role_from_entry(item).strip()
    if not role:
        return False
    return any(key in item for key in ("text", "content", "parts", "message"))


def _message_from_node(node: dict[str, Any], *, node_id: str) -> dict[str, Any] | None:
    message = node.get("message")
    if not isinstance(message, dict):
        return None
    out = dict(message)
    if node_id and not _to_str(out.get("id")).strip():
        out["id"] = node_id
    node_timestamp = _extract_timestamp(node)
    if node_timestamp and not _extract_timestamp(out):
        out["timestamp"] = node_timestamp
    return out


def _timestamp_ordering_key(timestamp: str) -> tuple[int, str]:
    value = timestamp.strip()
    if not value:
        return (1, "")
    try:
        return (0, f"num:{float(value):020.6f}")
    except ValueError:
        return (0, f"str:{value}")


def _messages_from_mapping(row: dict[str, Any]) -> list[dict[str, Any]]:
    mapping_raw = row.get("mapping")
    if not isinstance(mapping_raw, dict):
        return []

    nodes: dict[str, dict[str, Any]] = {}
    for key, value in mapping_raw.items():
        if isinstance(value, dict):
            nodes[_to_str(key)] = value
    if not nodes:
        return []

    current_node = _to_str(row.get("current_node")).strip()
    if current_node and current_node in nodes:
        chain: list[dict[str, Any]] = []
        visited: set[str] = set()
        cursor = current_node
        while cursor and cursor not in visited and cursor in nodes:
            visited.add(cursor)
            node = nodes[cursor]
            message = _message_from_node(node, node_id=cursor)
            if message is not None:
                chain.append(message)
            cursor = _to_str(node.get("parent")).strip()
        chain.reverse()
        if chain:
            return chain

    fallback: list[dict[str, Any]] = []
    for node_id, node in nodes.items():
        message = _message_from_node(node, node_id=node_id)
        if message is not None:
            fallback.append(message)
    fallback.sort(
        key=lambda item: (
            _timestamp_ordering_key(_extract_timestamp(item)),
            _to_str(item.get("id")).strip(),
        )
    )
    return fallback


def _canonical_items(items: list[Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entry = item
        if isinstance(item.get("message"), dict) and not _to_str(item.get("role")).strip():
            entry = item["message"]
        role = _normalize_role(_role_from_entry(entry))
        if not role:
            continue
        text = _entry_text(entry, for_observation=role not in {"user", "assistant"}).strip()
        if not text:
            continue
        out.append(
            {
                "role": role,
                "text": text,
                "timestamp": _extract_timestamp(entry) or _extract_timestamp(item),
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


def _items_from_row(row: dict[str, Any]) -> list[Any]:
    for key in ("messages", "events", "contents"):
        value = row.get(key)
        if isinstance(value, list):
            return value
    mapping_items = _messages_from_mapping(row)
    if mapping_items:
        return mapping_items
    conversation = row.get("conversation")
    if isinstance(conversation, dict):
        nested_items = _items_from_row(conversation)
        if nested_items:
            return nested_items
    if _is_message_like(row):
        return [row]
    return []


def _normalize_row_count(row: dict[str, Any]) -> int:
    items = _items_from_row(row)
    if items:
        return len(items)
    return 1


def _rows_from_parsed(parsed: Any) -> list[dict[str, Any]]:
    if isinstance(parsed, list):
        if all(_is_message_like(item) for item in parsed):
            return [{"messages": parsed}]
        rows: list[dict[str, Any]] = []
        for idx, item in enumerate(parsed, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"JSON array item {idx} must be an object")
            rows.extend(_rows_from_parsed(item))
        return rows

    if isinstance(parsed, dict):
        conversations = parsed.get("conversations")
        if isinstance(conversations, list):
            rows: list[dict[str, Any]] = []
            for idx, item in enumerate(conversations, start=1):
                if not isinstance(item, dict):
                    raise ValueError(f"conversations[{idx}] must be an object")
                rows.append(item)
            return rows
        if _is_message_like(parsed):
            return [{"messages": [parsed]}]
        return [parsed]

    raise ValueError("Input must be a JSON object, JSON array, or JSONL objects")


def _load_rows(path: Path) -> tuple[list[dict[str, Any]], int, int]:
    raw_text = path.read_text(encoding="utf-8-sig")
    stripped = raw_text.strip()
    if not stripped:
        return [], 0, 0

    rows: list[dict[str, Any]] = []
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
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
            rows.extend(_rows_from_parsed(item))
    else:
        rows = _rows_from_parsed(parsed)

    input_rows = len(rows)
    input_messages = sum(_normalize_row_count(row) for row in rows)
    return rows, input_rows, input_messages


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

        items = _items_from_row(row)
        if items:
            partial_turns = _pair_items_to_turns(
                _canonical_items(items),
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
            converted.append(
                {
                    "session_id": row_session,
                    "thread_id": row_thread,
                    "turn_id": _to_str(turn.get("turn_id")).strip() or next_turn_id(),
                    "user_text": _to_str(turn.get("user_text")).strip(),
                    "assistant_text": _to_str(turn.get("assistant_text")).strip(),
                    "observations": _normalize_observations(turn.get("observations")),
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
    parser = argparse.ArgumentParser(description="Convert conversation exports into operator inbox JSONL turns.")
    parser.add_argument("--input", required=True, help="Path to JSON or JSONL export")
    parser.add_argument("--output", required=True, help="Path to output JSONL")
    parser.add_argument("--session-id", default="import", help="Default session_id for output rows")
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
        default_session_id=_to_str(args.session_id).strip() or "import",
        default_thread_id=_to_str(thread_id).strip() or input_path.stem,
    )
    _write_jsonl(output_path, turns)
    print(f"input_rows={input_rows} input_messages={input_messages} output_turns={len(turns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
