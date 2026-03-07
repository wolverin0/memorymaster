from __future__ import annotations

from memorymaster.models import Claim

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "candidate": {"confirmed", "conflicted", "superseded", "archived"},
    "confirmed": {"stale", "superseded", "conflicted", "archived"},
    "stale": {"confirmed", "superseded", "conflicted", "archived"},
    "superseded": {"archived"},
    "conflicted": {"confirmed", "superseded", "stale", "archived"},
    "archived": set(),
}


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, set())


def transition_claim(
    store,
    claim_id: int,
    to_status: str,
    reason: str,
    event_type: str = "transition",
    replaced_by_claim_id: int | None = None,
) -> Claim:
    claim = store.get_claim(claim_id, include_citations=False)
    if claim is None:
        raise ValueError(f"Claim {claim_id} does not exist.")
    if claim.status == to_status:
        return claim
    if not can_transition(claim.status, to_status):
        raise ValueError(f"Invalid transition: {claim.status} -> {to_status}")
    if to_status == "superseded" and replaced_by_claim_id is None:
        raise ValueError("Superseded transition requires replaced_by_claim_id.")
    return store.apply_status_transition(
        claim,
        to_status=to_status,
        reason=reason,
        event_type=event_type,
        replaced_by_claim_id=replaced_by_claim_id,
    )
