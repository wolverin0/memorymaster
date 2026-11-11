"""Gate for text about to leave toward an external LLM provider (S-2/S-3/S-4).

Literal secret redaction alone lets private IPs, absolute paths, third-party emails and encoded
credentials out. ``profile.egress.prepare_transcript_egress`` redacts those and flags a text that still
carries a credential-grade finding (``blocked``). Nothing here swallows an ImportError: without the
egress filter nothing may leave (fail-closed).
"""

from __future__ import annotations

from typing import Any

from memorymaster.profile.egress import prepare_transcript_egress

WITHHELD = "[WITHHELD:credential_grade]"


def export_text(text: str) -> str | None:
    """``text`` as it may leave the machine, or ``None`` when it must be withheld."""
    egress = prepare_transcript_egress(text)
    return None if egress.blocked else egress.text


def export_lines(text: str) -> tuple[str, int]:
    """Like ``export_text`` for a multi-turn excerpt: a blocked excerpt loses only its blocked lines.

    Returns ``(safe_text, withheld_line_count)``. The per-line pass runs on the original lines
    (an already-redacted text would be refused for its own markers).
    shortcut: a credential spanning several lines is judged line by line, upgrade if one shows up.
    """
    whole = prepare_transcript_egress(text)
    if not whole.blocked:
        return whole.text, 0
    kept: list[str] = []
    withheld = 0
    for line in text.split("\n"):
        safe = export_text(line)
        if safe is None:
            withheld += 1
            safe = WITHHELD
        kept.append(safe)
    return "\n".join(kept), withheld


def export_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Provider-safe copies of ``messages``; a blocked message keeps its id/role with a marker text."""
    safe: list[dict[str, Any]] = []
    withheld = 0
    for message in messages:
        text = export_text(str(message.get("text", "")))
        if text is None:
            withheld += 1
            text = WITHHELD
        safe.append({**message, "text": text})
    return safe, withheld
