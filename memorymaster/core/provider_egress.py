"""Gate for text about to leave toward an external LLM provider (S-2/S-3/S-4).

Literal secret redaction alone lets private IPs, absolute paths, third-party emails and encoded
credentials out. ``profile.egress`` redacts those and flags a text that still carries a
credential-grade finding (``blocked``). Two entry points there:

* raw transcript text (PreCompact residual, session-end): ``prepare_claim_egress`` as-is;
* STORED dreaming captures, whose text already carries our own ``[REDACTED:<name>]`` markers:
  ``prepare_transcript_egress``.

The importing of the egress module is never guarded: without the filter nothing may leave
(fail-closed). An exception while gating ONE text withholds that text only.
"""

from __future__ import annotations

import re
from typing import Any

from memorymaster.profile.egress import ClaimEgress, prepare_claim_egress, prepare_transcript_egress

WITHHELD = "[WITHHELD:credential_grade]"
GATE_ERROR = "[WITHHELD:gate_error]"
# The patterns are quadratic on an unbroken run: bound both the chunk and the longest token.
MAX_CHUNK = 20_000
MAX_TOKEN = 4_000
_LONG_TOKEN = re.compile(r"\S{%d,}" % (MAX_TOKEN + 1))
_PIECE = re.compile(r"\S+\s*")


def _chunks(text: str) -> list[str]:
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for match in _PIECE.finditer(text):
        piece = match.group(0)
        if size + len(piece) > MAX_CHUNK and current:
            pieces.append("".join(current))
            current, size = [], 0
        current.append(piece)
        size += len(piece)
    pieces.append("".join(current))
    return pieces


def _gate(text: str, *, stored: bool) -> str | None:
    """Provider-safe ``text`` or ``None`` when blocked; may raise (callers contain it per text)."""
    if _LONG_TOKEN.search(text):  # a blob, not prose: also the quadratic worst case
        return None
    prepare = prepare_transcript_egress if stored else prepare_claim_egress
    parts = _chunks(text) if len(text) > MAX_CHUNK else [text]
    safe: list[str] = []
    for part in parts:
        egress: ClaimEgress = prepare(part)
        if egress.blocked:
            return None
        safe.append(egress.text)
    return "".join(safe)


def export_text(text: str) -> str | None:
    """Raw ``text`` as it may leave the machine, or ``None`` when it must be withheld."""
    try:
        return _gate(text, stored=False)
    except Exception:  # noqa: BLE001 - a gate fault withholds this text, never sends it
        return None


def export_lines(text: str) -> tuple[str, int]:
    """Like ``export_text`` for a multi-turn raw excerpt: a blocked excerpt loses only its blocked lines.

    Returns ``(safe_text, withheld_line_count)``; ``safe_text`` is ``""`` when nothing is left to send.
    The per-line pass runs on the original lines (an already-redacted text would be refused for its
    own markers).
    shortcut: a credential spanning several lines is judged line by line, upgrade if one shows up.
    """
    whole = export_text(text)
    if whole is not None:
        return whole, 0
    kept: list[str] = []
    withheld = 0
    for line in text.split("\n"):
        safe = export_text(line)
        if safe is None:
            withheld += 1
            safe = WITHHELD
        kept.append(safe)
    if all(line == WITHHELD or not line.strip() for line in kept):
        return "", withheld
    return "\n".join(kept), withheld


def export_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Provider-safe copies of STORED capture ``messages``; a withheld one keeps its id/role with a marker."""
    safe: list[dict[str, Any]] = []
    withheld = 0
    for message in messages:
        try:
            text = _gate(str(message.get("text", "")), stored=True)
            marker = WITHHELD
        except Exception:  # noqa: BLE001 - one faulty message must not take the batch down
            text, marker = None, GATE_ERROR
        if text is None:
            withheld += 1
            text = marker
        safe.append({**message, "text": text})
    return safe, withheld


def quote_in_sent_text(quote: str, text: str) -> bool:
    """Whether ``quote`` is a substring of the text the provider saw for the stored message ``text``."""
    sent = export_messages([{"text": text}])[0][0]["text"]
    return bool(quote) and quote in sent
