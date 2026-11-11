"""Egress gate for the consolidation request (candidates + reference claims).

``consolidation_prompt`` already applies ``source_review.safe_context`` (literal and encoded secrets,
private IPv4 and Windows paths, home paths, long digit runs), but not emails, POSIX home paths,
ssh shapes, public IP:port or a credential-grade finding that survives. This gate adds those. A
withheld candidate is not sent and gets an ``ignore`` decision (the existing no-action verdict); a
withheld reference claim is simply left out of the context.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from memorymaster.core.provider_egress import export_messages, export_stored
from memorymaster.dreaming.models import ConsolidationResult, DreamCandidate, DreamDecision

_CANDIDATE_FIELDS = ("text", "subject", "predicate", "object_value", "evidence_quote")
_REFERENCE_FIELDS = ("text", "subject", "predicate", "object_value", "memory_key")
WITHHELD_RATIONALE = "withheld by the egress gate; not sent to the provider"


def _gate_fields(fields: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any] | None:
    changed: dict[str, Any] = {}
    for name in names:
        value = fields.get(name)
        if isinstance(value, str) and value:
            safe = export_stored(value)
            if safe is None:
                return None
            changed[name] = safe
    return changed


def _gate_candidate(candidate: DreamCandidate) -> DreamCandidate | None:
    changed = _gate_fields({name: getattr(candidate, name, None) for name in _CANDIDATE_FIELDS}, _CANDIDATE_FIELDS)
    if changed is None:
        return None
    context = getattr(candidate, "source_context", None)
    if isinstance(context, dict) and isinstance(context.get("messages"), list):
        # The capture's messages ride along as evidence: a withheld message keeps its id with a marker.
        changed["source_context"] = {**context, "messages": export_messages(context["messages"])[0]}
    return replace(candidate, **changed)


def gate_consolidation(
    candidates: list[DreamCandidate],
    references: list[dict[str, Any]],
) -> tuple[list[DreamCandidate], list[DreamDecision], list[dict[str, Any]], int]:
    """(candidates to send, ignore decisions for the withheld ones, references to send, withheld count)."""
    sent: list[DreamCandidate] = []
    ignored: list[DreamDecision] = []
    for candidate in candidates:
        try:
            safe = _gate_candidate(candidate)
        except Exception:  # noqa: BLE001 - a gate fault withholds this candidate only
            safe = None
        if safe is None:
            ignored.append(DreamDecision(candidate.candidate_id, "ignore", WITHHELD_RATIONALE, 1.0))
        else:
            sent.append(safe)
    kept: list[dict[str, Any]] = []
    dropped = 0
    for reference in references:
        changed = _gate_fields(reference, _REFERENCE_FIELDS)
        if changed is None:
            dropped += 1
        else:
            kept.append({**reference, **changed})
    return sent, ignored, kept, len(ignored) + dropped


def with_ignored(result: ConsolidationResult | None, ignored: list[DreamDecision]) -> ConsolidationResult:
    """The provider's verdicts plus the ``ignore`` verdicts of the candidates that were never sent."""
    decisions = (*(result.decisions if result else ()), *ignored)
    return ConsolidationResult(tuple(decisions), result.usage if result else None)
