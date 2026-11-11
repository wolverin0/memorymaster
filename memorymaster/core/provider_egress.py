"""Gate for text about to leave toward an external LLM provider (S-2/S-3/S-4).

Literal secret redaction alone lets private IPs, absolute paths, third-party emails and encoded
credentials out. ``profile.egress`` redacts those and flags a text that still carries a
credential-grade finding (``blocked``). Two entry points there:

* raw transcript text (PreCompact residual, session-end): ``prepare_claim_egress`` as-is;
* STORED dreaming captures, whose text already carries our own ``[REDACTED:<name>]`` markers:
  ``prepare_transcript_egress``.

The importing of the egress module is never guarded: without the filter nothing may leave
(fail-closed). An exception while gating ONE text withholds that text only.

Ungated, dormant: gate before enabling (census; flag that enables each):
  knowledge/rule_miner.py          MEMORYMASTER_STEWARD_RULE_MINING / MEMORYMASTER_STOP_RULE_MINING
  knowledge/wiki_engine.py         MEMORYMASTER_WIKI_ABSORB (vault_curator.py / vault_linter.py: CLI only)
  recall/llm_rerank.py             MEMORYMASTER_LLM_RERANK
  knowledge/skills.py              MEMORYMASTER_SKILL_REVIEW
  knowledge/entity_extractor.py    MEMORYMASTER_ENTITY_LLM
  CLI-only: govern/auto_resolver.py, recall/selfcheck.py, govern/llm_steward.py,
  govern/jobs/compact_summaries.py, workflow_intelligence/classification.py,
  core/llm_budget.py (wrapper), scripts/label_prompts_with_judge.py
tests/test_provider_egress_sites.py fails when a NEW call_llm call site appears outside its allowlist.
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


def export_stored(text: str) -> str | None:
    """Like ``export_text`` for STORED text that may carry our own ``[REDACTED:<name>]`` markers."""
    try:
        return _gate(text, stored=True)
    except Exception:  # noqa: BLE001 - a gate fault withholds this text, never sends it
        return None


def redacted_spans(original: str, gated: str) -> frozenset[str]:
    """The substrings of ``original`` that the gate replaced to produce ``gated``."""
    from difflib import SequenceMatcher

    ops = SequenceMatcher(None, original, gated, autojunk=False).get_opcodes()
    return frozenset(original[i1:i2] for tag, i1, i2, _j1, _j2 in ops if tag in {"replace", "delete"})


def export_pair(first: str, second: str) -> tuple[str, str] | None:
    """Gate two claims to be compared by a model; ``None`` when the pair must not be sent.

    A redaction marker can make two different values look equal, so a pair is sent only when the gate
    changed neither text, or replaced the very same spans in both.
    """
    safe_first, safe_second = export_stored(first), export_stored(second)
    if safe_first is None or safe_second is None:
        return None
    if safe_first == first and safe_second == second:
        return safe_first, safe_second
    if max(len(first), len(second)) > 4_000:  # the span diff is quadratic: not worth it for a blob
        return None
    if redacted_spans(first, safe_first) != redacted_spans(second, safe_second):
        return None
    return safe_first, safe_second


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
