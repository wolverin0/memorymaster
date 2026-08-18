"""Govern mutable-state admission and explicit supersession intent.

Keeps the service facade small while preserving candidate-only writes,
identity-bound replacement proposals, and auditable volatility overrides.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from memorymaster.core.intake_policy import IntakeRejected
from memorymaster.core.models import Claim
from memorymaster.core.temporal_policy import (
    MutableStateDecision,
    classify_mutable_state,
)


@dataclass(frozen=True, slots=True)
class IngestGovernanceDecision:
    mutable_state: MutableStateDecision
    supersession_target: Claim | None
    requested_volatility: str


def prepare_ingest_governance(
    store: Any,
    *,
    text: str,
    claim_type: str | None,
    supersedes_claim_id: int | None,
    tenant_id: str | None,
    scope: str,
    volatility: str,
    valid_until: str | None,
    visibility: str,
    source_agent: str | None,
) -> IngestGovernanceDecision:
    mutable_state = govern_mutable_state(
        text=text,
        claim_type=claim_type,
        scope=scope,
        volatility=volatility,
        valid_until=valid_until,
    )
    target = resolve_supersession_target(
        store,
        supersedes_claim_id,
        tenant_id=tenant_id,
        scope=scope,
        visibility=visibility,
        source_agent=source_agent,
    )
    return IngestGovernanceDecision(mutable_state, target, volatility)


def govern_mutable_state(
    *,
    text: str,
    claim_type: str | None,
    scope: str,
    volatility: str,
    valid_until: str | None,
) -> MutableStateDecision:
    decision = classify_mutable_state(
        text=text,
        claim_type=claim_type,
        volatility=volatility,
        valid_until=valid_until,
    )
    if decision.is_mutable and scope.strip().lower() == "global":
        raise IntakeRejected(
            "mutable operational state cannot use global scope; use user or "
            "project:<slug> scope and supply valid_until when known.",
            rule="mutable_state_scope",
            reason="global_mutable_state",
        )
    return decision


def resolve_supersession_target(
    store: Any,
    claim_id: int | None,
    *,
    tenant_id: str | None,
    scope: str,
    visibility: str,
    source_agent: str | None,
) -> Claim | None:
    """Resolve a proposed replacement without crossing identity boundaries."""
    if claim_id is None:
        return None
    if isinstance(claim_id, bool) or not isinstance(claim_id, int) or claim_id <= 0:
        raise ValueError("supersession target must be a positive claim id")
    target = store.get_claim(claim_id, include_citations=False)
    if target is None or (tenant_id is not None and target.tenant_id != tenant_id):
        raise ValueError(f"supersession target {claim_id} does not exist")
    if (target.scope, target.visibility, target.source_agent) != (
        scope,
        visibility,
        source_agent,
    ):
        raise ValueError("supersession target is outside the claim identity boundary")
    if target.status in {"archived", "superseded"}:
        raise ValueError("supersession target is not active")
    return target


def record_mutable_override(
    store: Any,
    claim: Claim,
    decision: MutableStateDecision,
    requested_volatility: str,
) -> None:
    if not decision.forced_high:
        return
    store.record_event(
        claim_id=claim.id,
        event_type="policy_decision",
        details="mutable_state:forced_high_volatility",
        payload={
            "rule": "mutable_state_expiry",
            "requested_volatility": requested_volatility,
            "effective_volatility": "high",
            "reason": "structured_valid_until_missing",
        },
    )


def apply_post_ingest_governance(
    store: Any,
    claim: Claim,
    decision: IngestGovernanceDecision,
    confidence: float,
) -> None:
    record_mutable_override(
        store,
        claim,
        decision.mutable_state,
        decision.requested_volatility,
    )
    queue_supersession_proposal(
        store,
        decision.supersession_target,
        claim,
        confidence,
    )


def queue_supersession_proposal(
    store: Any,
    target: Claim | None,
    replacement: Claim,
    confidence: float,
) -> None:
    """Queue steward review; direct ingest never rewrites confirmed truth."""
    if target is None:
        return
    # Dedup on the TARGET, not on the (target, replacement) pair. A claim can
    # only be superseded once: whichever replacement is approved first retires
    # it, and every further proposal against the same target is then guaranteed
    # to fail with "already superseded". Two dream-worker proposals filed one
    # second apart against claim 123737 is exactly how that happened, and the
    # failure used to latch into an unrecoverable state.
    for event in store.list_events(
        claim_id=target.id, event_type="policy_decision", limit=100
    ):
        if event.details == "steward_proposal:superseded_candidate":
            return
    store.record_event(
        claim_id=target.id,
        event_type="policy_decision",
        from_status=target.status,
        to_status="superseded",
        details="steward_proposal:superseded_candidate",
        payload={
            "source": "direct-ingest",
            "proposal_type": "review_queue_item",
            "decision": "superseded_candidate",
            "proposed_status": "superseded",
            "priority": confidence,
            "apply_requested": False,
            "reasons": [{
                "code": "explicit_supersession_intent",
                "detail": "Caller identified this candidate as a replacement.",
            }],
            "replaced_by_claim_id": replacement.id,
            "candidate_id": f"ingest:{replacement.id}",
        },
    )
