"""Regression test for auto-ingest hook transcript schema adapter.

Guards against the bug where the hook read `entry.get("role")` at the
top level, but Claude Code transcripts wrap the role inside
`entry["message"]["role"]`. The fix must accept BOTH shapes.

Tests the extraction pattern used by:
  - ~/.claude/hooks/memorymaster-auto-ingest.py (deployed)
  - memorymaster/config_templates/hooks/memorymaster-auto-ingest.py (ships to new installs)
  - scripts/llm_benchmark.py (benchmark harness)
"""

from __future__ import annotations

import json
from pathlib import Path


def _extract(transcript_path: Path, max_chars: int = 3000) -> str:
    """Mirror of the hook's extraction logic — if this test passes, the hook pattern is correct."""
    messages: list[str] = []
    lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in reversed(lines[-200:]):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = entry.get("message") if isinstance(entry.get("message"), dict) else entry
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                c.get("text", "")
                for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            )
        text = text.strip()
        if text and len(text) > 30:
            messages.append(text[:500])
            if sum(len(m) for m in messages) > max_chars:
                break
    return "\n---\n".join(reversed(messages))


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n",
        encoding="utf-8",
    )


def test_wrapped_schema_is_parsed(tmp_path: Path) -> None:
    """Claude Code current schema: role+content wrapped in entry['message']."""
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, [
        {"type": "assistant", "message": {"role": "assistant", "content": "This is a substantive assistant message with more than 30 characters."}},
        {"type": "user", "message": {"role": "user", "content": "a user msg"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Another assistant reply that is long enough to include."}]}},
    ])
    out = _extract(p)
    assert "substantive assistant message" in out
    assert "Another assistant reply" in out
    assert "a user msg" not in out


def test_flat_schema_is_parsed(tmp_path: Path) -> None:
    """Legacy/alternate schema: role at top level."""
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, [
        {"role": "assistant", "content": "Flat-schema assistant message with enough characters to pass the 30-char filter."},
        {"role": "user", "content": "flat user msg"},
    ])
    out = _extract(p)
    assert "Flat-schema assistant message" in out
    assert "flat user msg" not in out


def test_mixed_schema_is_parsed(tmp_path: Path) -> None:
    """Both shapes in the same file — extractor must handle both without losing entries."""
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, [
        {"role": "assistant", "content": "First assistant reply in flat schema format with enough length."},
        {"message": {"role": "assistant", "content": "Second reply in wrapped schema format with enough length."}},
    ])
    out = _extract(p)
    assert "First assistant reply" in out
    assert "Second reply" in out


def test_short_messages_are_filtered(tmp_path: Path) -> None:
    """The hook ignores assistant messages shorter than 30 chars (the 'ok' filter)."""
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, [
        {"message": {"role": "assistant", "content": "short"}},
        {"message": {"role": "assistant", "content": "ok"}},
    ])
    out = _extract(p)
    assert out == ""


def test_empty_transcript(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    p.write_text("", encoding="utf-8")
    assert _extract(p) == ""


def test_malformed_lines_are_skipped(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    p.write_text(
        "not json at all\n"
        + json.dumps({"message": {"role": "assistant", "content": "Valid assistant message with enough length to pass."}})
        + "\n{malformed\n",
        encoding="utf-8",
    )
    out = _extract(p)
    assert "Valid assistant message" in out


def test_deployed_hook_has_adapter_pattern() -> None:
    """Static check: the deployed hook must use the wrapped-schema adapter."""
    import os

    p = Path(os.path.expanduser("~/.claude/hooks/memorymaster-auto-ingest.py"))
    if not p.exists():
        return  # not installed in this environment — skip silently
    src = p.read_text(encoding="utf-8", errors="replace")
    assert 'entry.get("message")' in src, (
        "auto-ingest hook missing wrapped-schema adapter — "
        "must normalize entry['message'] before checking role"
    )


def test_template_hook_has_adapter_pattern() -> None:
    """Static check: the shipped template must use the wrapped-schema adapter."""
    p = Path(__file__).parent.parent / "memorymaster" / "config_templates" / "hooks" / "memorymaster-auto-ingest.py"
    assert p.exists(), f"template not found at {p}"
    src = p.read_text(encoding="utf-8", errors="replace")
    assert 'entry.get("message")' in src, (
        "template hook missing wrapped-schema adapter — "
        "new installs will silently extract zero learnings"
    )
