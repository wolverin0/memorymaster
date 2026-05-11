from __future__ import annotations

import logging
import os

from memorymaster.models import Claim

logger = logging.getLogger(__name__)

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


def _wiki_autopromote_after_validator(store, claim_id: int, event_type: str) -> None:
    if event_type != "validator":
        return
    raw_threshold = os.environ.get("MEMORYMASTER_WIKI_AUTOPROMOTE_THRESHOLD", "3")
    try:
        threshold = int(raw_threshold)
    except ValueError:
        logger.warning("invalid wiki autopromote threshold: %r", raw_threshold)
        return
    if threshold <= 0:
        return

    try:
        events = store.list_events(claim_id=claim_id, event_type="validator", limit=threshold + 1)
        if len({event.id for event in events}) != threshold:
            return
        from memorymaster.wiki_engine import absorb_single_claim

        absorb_single_claim(claim_id, db_path=getattr(store, "db_path", None))
    except Exception as exc:
        logger.warning("wiki autopromote failed for claim %s: %s", claim_id, exc)


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
    updated = store.apply_status_transition(
        claim,
        to_status=to_status,
        reason=reason,
        event_type=event_type,
        replaced_by_claim_id=replaced_by_claim_id,
    )
    _wiki_autopromote_after_validator(store, updated.id, event_type)
    return updated
