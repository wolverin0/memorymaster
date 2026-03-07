from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

try:
    from scripts import claude_to_turns, conversation_to_turns
except ImportError:
    import claude_to_turns  # type: ignore[no-redef]
    import conversation_to_turns  # type: ignore[no-redef]

_FORMAT_CHOICES = ("auto", "openai", "claude", "gemini", "conversation")
_CONVERSATION_PARSERS = {"openai", "gemini", "conversation"}


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _detect_format_from_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        conversations = payload.get("conversations")
        if isinstance(conversations, list):
            return "openai"
        if isinstance(payload.get("mapping"), dict) or _to_str(payload.get("current_node")).strip():
            return "openai"
        if isinstance(payload.get("contents"), list):
            return "gemini"
        messages = payload.get("messages")
        if isinstance(messages, list):
            if any(isinstance(item, dict) and isinstance(item.get("parts"), list) for item in messages):
                return "gemini"
            if any(
                isinstance(item, dict) and _to_str(item.get("role")).strip().lower() in {"model", "user"}
                for item in messages
            ):
                return "claude"
        if _to_str(payload.get("role")).strip().lower() == "model":
            return "gemini"
        return "claude"

    if isinstance(payload, list):
        dict_items = [item for item in payload if isinstance(item, dict)]
        if not dict_items:
            return "claude"
        if any(isinstance(item.get("parts"), list) for item in dict_items):
            return "gemini"
        if any(_to_str(item.get("role")).strip().lower() == "model" for item in dict_items):
            return "gemini"
        if any(isinstance(item.get("mapping"), dict) for item in dict_items):
            return "openai"
        return "claude"

    return "claude"


def _detect_format(path: Path) -> str:
    raw_text = path.read_text(encoding="utf-8-sig")
    stripped = raw_text.strip()
    if not stripped:
        return "claude"
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        for line in raw_text.splitlines():
            payload = line.strip()
            if not payload:
                continue
            try:
                sample = json.loads(payload)
            except json.JSONDecodeError:
                continue
            return _detect_format_from_payload(sample)
        return "claude"
    return _detect_format_from_payload(parsed)


def _resolve_parser(format_hint: str, *, input_path: Path) -> str:
    normalized = _to_str(format_hint).strip().lower()
    if normalized not in _FORMAT_CHOICES:
        raise ValueError(f"Unsupported format hint: {format_hint}")
    if normalized != "auto":
        return normalized
    return _detect_format(input_path)


def _parser_module_name(format_name: str) -> str:
    normalized = _to_str(format_name).strip().lower()
    if normalized in _CONVERSATION_PARSERS:
        return "conversation"
    if normalized == "claude":
        return "claude"
    return "conversation"


def load_rows(
    path: Path,
    *,
    format_hint: str = "auto",
) -> tuple[list[dict[str, Any]], int, int, str]:
    format_name = _resolve_parser(format_hint, input_path=path)
    parser_name = _parser_module_name(format_name)
    if parser_name == "claude":
        rows, input_rows, input_messages = claude_to_turns._load_rows(path)  # noqa: SLF001
        return rows, input_rows, input_messages, "claude"
    rows, input_rows, input_messages = conversation_to_turns._load_rows(path)  # noqa: SLF001
    if format_name in {"openai", "gemini"}:
        return rows, input_rows, input_messages, format_name
    return rows, input_rows, input_messages, "conversation"


def convert_rows(
    rows: list[dict[str, Any]],
    *,
    default_session_id: str,
    default_thread_id: str,
    parser_name: str = "conversation",
) -> list[dict[str, Any]]:
    parser = _parser_module_name(parser_name)
    if parser == "claude":
        return claude_to_turns._convert_rows(  # noqa: SLF001
            rows,
            default_session_id=default_session_id,
            default_thread_id=default_thread_id,
        )
    return conversation_to_turns._convert_rows(  # noqa: SLF001
        rows,
        default_session_id=default_session_id,
        default_thread_id=default_thread_id,
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    conversation_to_turns._write_jsonl(path, rows)  # noqa: SLF001


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Import conversation JSON/JSONL exports and normalize to operator inbox turns. "
            "Supports OpenAI, Claude, and Gemini variants via auto-detection or --format override."
        )
    )
    parser.add_argument("--input", required=True, help="Path to conversation export (JSON or JSONL)")
    parser.add_argument("--output", required=True, help="Path to output JSONL")
    parser.add_argument(
        "--format",
        choices=_FORMAT_CHOICES,
        default="auto",
        help=(
            "Input format hint. 'auto' detects format from payload shape. "
            "'openai' and 'gemini' route through conversation_to_turns, 'claude' routes through claude_to_turns."
        ),
    )
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

    thread_id = _to_str(args.thread_id).strip() or input_path.stem
    rows, input_rows, input_messages, parser_name = load_rows(
        input_path,
        format_hint=args.format,
    )
    turns = convert_rows(
        rows,
        default_session_id=_to_str(args.session_id).strip() or "import",
        default_thread_id=thread_id,
        parser_name=parser_name,
    )
    write_jsonl(output_path, turns)
    print(
        f"format={parser_name} input_rows={input_rows} input_messages={input_messages} output_turns={len(turns)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
