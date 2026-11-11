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

from memorymaster.profile.egress import (
    ClaimEgress,
    prepare_claim_egress,
    prepare_indexed_egress,
    prepare_transcript_egress,
)

WITHHELD = "[WITHHELD:credential_grade]"
GATE_ERROR = "[WITHHELD:gate_error]"
LONG_TOKEN = "[WITHHELD:long_token]"
# The patterns are quadratic on an unbroken run (200 KB of 1,000-char tokens: ~1.5 s): such a run is
# a blob, not prose, and is replaced by a marker while the rest of the text is gated normally.
MAX_TOKEN = 1_000
MAX_INPUT = 20_000  # callers that cut to a few hundred characters anyway bound the gate's input
_LONG_TOKEN = re.compile(r"\S{%d,}" % (MAX_TOKEN + 1))


def _gate(text: str, *, stored: bool, trusted: bool = False) -> str | None:
    """Provider-safe ``text`` or ``None`` when blocked; may raise (callers contain it per text)."""
    text = _LONG_TOKEN.sub(LONG_TOKEN, text)
    egress: ClaimEgress = prepare_transcript_egress(text, trusted=trusted) if stored else prepare_claim_egress(text)
    return None if egress.blocked else egress.text


def export_text(text: str) -> str | None:
    """Raw ``text`` as it may leave the machine, or ``None`` when it must be withheld."""
    try:
        return _gate(text, stored=False)
    except Exception:  # noqa: BLE001 - a gate fault withholds this text, never sends it
        return None


def gate_stored(text: str, *, trusted: bool = False) -> str | None:
    """``export_stored`` that RAISES on a gate fault, for callers that must tell a fault from a block."""
    return _gate(text, stored=True, trusted=trusted)


def export_stored(text: str, *, trusted: bool = False) -> str | None:
    """Like ``export_text`` for STORED text that may carry our own ``[REDACTED:<name>]`` markers.

    ``trusted``: the text was built from output of this gate (a model-derived candidate), so every
    well-formed marker is neutralised even when letters touch it (the gate itself can leave
    ``at[REDACTED:home_path_unix]proj`` behind).
    """
    try:
        return _gate(text, stored=True, trusted=trusted)
    except Exception:  # noqa: BLE001 - a gate fault withholds this text, never sends it
        return None


def export_pair(first: str, second: str) -> tuple[str, str] | None:
    """Gate two stored claims a model will compare; ``None`` when the pair must not be sent.

    Redacted values get stable pseudonyms across both texts (``[REDACTED:private_ipv4#1]``: the same
    value the same index, another value another), so the model can still tell them apart.
    """
    try:
        pair = prepare_indexed_egress([_LONG_TOKEN.sub(LONG_TOKEN, first), _LONG_TOKEN.sub(LONG_TOKEN, second)])
    except Exception:  # noqa: BLE001 - a gate fault withholds the pair
        return None
    if any(item.blocked for item in pair):
        return None
    return pair[0].text, pair[1].text


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
