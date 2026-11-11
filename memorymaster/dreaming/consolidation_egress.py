"""Egress gate for the consolidation request (candidates + reference claims).

``consolidation_prompt`` already applies ``source_review.safe_context`` (literal and encoded secrets,
private IPv4 and Windows paths, home paths, long digit runs), but not emails, POSIX home paths,
ssh shapes, public IP:port or a credential-grade finding that survives. This gate adds those. A
candidate withheld for a credential-grade finding is not sent and gets an ``ignore`` decision (the
existing no-action verdict, marked ``WITHHELD_RATIONALE`` so the reward ledger skips it); a gate FAULT
is never terminal: it raises ``EgressGateFault`` and the batch follows the worker's retry path. A
withheld reference claim is simply left out of the context.

Candidate fields are built from text this gate already passed (a model quoted it back), so their
markers are neutralised unconditionally (``trusted``): re-gating must not withhold a fact because the
first pass left a marker touching letters. Reference claims and capture messages are stored text and
keep the strict spliced-marker rule.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from memorymaster.core.provider_egress import export_messages, gate_stored
from memorymaster.dreaming.models import ConsolidationResult, DreamCandidate, DreamDecision

_CANDIDATE_FIELDS = ("text", "subject", "predicate", "object_value", "evidence_quote")
_REFERENCE_FIELDS = ("text", "subject", "predicate", "object_value", "memory_key")
WITHHELD_RATIONALE = "withheld by the egress gate; not sent to the provider"


class EgressGateFault(RuntimeError):
    """The gate itself failed on a candidate: nothing was decided, the capture is retried."""


def _gate_fields(fields: dict[str, Any], names: tuple[str, ...], *, trusted: bool) -> dict[str, Any] | None:
    changed: dict[str, Any] = {}
    for name in names:
        value = fields.get(name)
        if isinstance(value, str) and value:
            safe = gate_stored(value, trusted=trusted)  # raises on a fault
            if safe is None:
                return None
            changed[name] = safe
    return changed


def _gate_candidate(candidate: DreamCandidate) -> DreamCandidate | None:
    changed = _gate_fields(
        {name: getattr(candidate, name, None) for name in _CANDIDATE_FIELDS}, _CANDIDATE_FIELDS, trusted=True
    )
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
        except Exception as exc:  # noqa: BLE001 - a fault is not a verdict: retry the capture later
            raise EgressGateFault(f"egress gate fault on candidate: {type(exc).__name__}") from exc
        if safe is None:
            ignored.append(DreamDecision(candidate.candidate_id, "ignore", WITHHELD_RATIONALE, 1.0))
        else:
            sent.append(safe)
    kept: list[dict[str, Any]] = []
    dropped = 0
    for reference in references:
        try:
            changed = _gate_fields(reference, _REFERENCE_FIELDS, trusted=False)
        except Exception:  # noqa: BLE001 - context only: a faulty reference is left out
            changed = None
        if changed is None:
            dropped += 1
        else:
            kept.append({**reference, **changed})
    return sent, ignored, kept, len(ignored) + dropped


def with_ignored(result: ConsolidationResult | None, ignored: list[DreamDecision]) -> ConsolidationResult:
    """The provider's verdicts plus the ``ignore`` verdicts of the candidates that were never sent."""
    decisions = (*(result.decisions if result else ()), *ignored)
    return ConsolidationResult(tuple(decisions), result.usage if result else None)
