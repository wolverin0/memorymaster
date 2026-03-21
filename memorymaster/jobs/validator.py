from __future__ import annotations

from memorymaster.config import get_config
from memorymaster.lifecycle import transition_claim
from memorymaster.models import Claim


def validation_score(claim: Claim, citation_count: int, prior_confidence: float) -> float:
    base = 0.35
    citation_bonus = min(citation_count * 0.12, 0.4)
    length_bonus = min(len(claim.text) / 240.0, 0.15)
    structure_bonus = 0.1 if claim.subject and claim.predicate and claim.object_value else 0.0
    raw = base + citation_bonus + length_bonus + structure_bonus
    blended = (raw * 0.75) + (prior_confidence * 0.25)
    return max(0.0, min(1.0, blended))


def _merge_claims(primary: list[Claim], secondary: list[Claim]) -> list[Claim]:
    seen: set[int] = set()
    merged: list[Claim] = []
    for claim in primary + secondary:
        if claim.id in seen:
            continue
        seen.add(claim.id)
        merged.append(claim)
    return merged


def run(
    store,
    limit: int = 200,
    min_citations: int = 1,
    min_score: float | None = None,
    revalidation_claims: list[Claim] | None = None,
    policy_mode: str = "legacy",
) -> dict[str, int]:
    cfg = get_config()
    if min_score is None:
        min_score = cfg.validation_threshold
    candidate_claims = store.find_by_status("candidate", limit=limit)
    due_revalidation_claims: list[Claim] = []
    if policy_mode != "legacy":
        due_revalidation_claims = [
            claim
            for claim in (revalidation_claims or [])
            if claim.status in {"confirmed", "stale", "conflicted"}
        ]

    claims = _merge_claims(candidate_claims, due_revalidation_claims)
    confirmed = 0
    conflicted = 0
    superseded = 0
    pending = 0
    staled = 0
    revalidated_healthy = 0

    # Batch-fetch citation counts to avoid N+1 queries
    citation_counts = (
        store.count_citations_batch([c.id for c in claims])
        if hasattr(store, "count_citations_batch")
        else {c.id: store.count_citations(c.id) for c in claims}
    )

    for claim in claims:
        is_revalidation = claim.status in {"confirmed", "stale", "conflicted"}
        citation_count = citation_counts.get(claim.id, 0)
        score = validation_score(claim, citation_count, prior_confidence=claim.confidence)
        store.set_confidence(claim.id, score, details=f"validator_score={score:.3f};citations={citation_count}")

        related = store.find_confirmed_by_tuple(
            subject=claim.subject,
            predicate=claim.predicate,
            scope=claim.scope,
            exclude_claim_id=claim.id,
        )

        duplicate = next((x for x in related if x.object_value == claim.object_value and x.object_value), None)
        if duplicate is not None and not is_revalidation:
            transition_claim(
                store,
                claim_id=claim.id,
                to_status="superseded",
                reason=f"duplicate_of_confirmed_claim:{duplicate.id}",
                event_type="validator",
                replaced_by_claim_id=duplicate.id,
            )
            superseded += 1
            continue

        if citation_count < min_citations or score < min_score:
            if is_revalidation and claim.status == "confirmed":
                transition_claim(
                    store,
                    claim_id=claim.id,
                    to_status="stale",
                    reason=f"revalidation_below_threshold score={score:.3f} citations={citation_count}",
                    event_type="validator",
                )
                staled += 1
            else:
                store.record_event(
                    claim_id=claim.id,
                    event_type="validator",
                    details="validation_pending_more_evidence",
                    payload={"score": score, "citation_count": citation_count, "revalidation": is_revalidation},
                )
            pending += 1
            continue

        conflict = next(
            (
                x
                for x in related
                if x.object_value
                and claim.object_value
                and x.object_value != claim.object_value
            ),
            None,
        )

        if conflict is not None and score <= (conflict.confidence + cfg.conflict_margin):
            if claim.status != "conflicted":
                transition_claim(
                    store,
                    claim_id=claim.id,
                    to_status="conflicted",
                    reason=f"conflicts_with_confirmed_claim:{conflict.id}",
                    event_type="validator",
                )
            else:
                store.record_event(
                    claim_id=claim.id,
                    event_type="validator",
                    from_status=claim.status,
                    to_status=claim.status,
                    details=f"revalidation_remains_conflicted:{conflict.id}",
                    payload={"score": score, "citation_count": citation_count},
                )
            conflicted += 1
            continue

        if claim.status != "confirmed":
            transition_claim(
                store,
                claim_id=claim.id,
                to_status="confirmed",
                reason=f"validated score={score:.3f} citations={citation_count}",
                event_type="validator",
            )
            confirmed += 1
        else:
            store.record_event(
                claim_id=claim.id,
                event_type="validator",
                from_status=claim.status,
                to_status=claim.status,
                details="revalidation_passed",
                payload={"score": score, "citation_count": citation_count},
            )
            revalidated_healthy += 1

        if conflict is not None:
            store.mark_superseded(
                old_claim_id=conflict.id,
                new_claim_id=claim.id,
                reason=f"superseded by claim:{claim.id}",
            )
            superseded += 1

    return {
        "processed": len(claims),
        "candidate_processed": len(candidate_claims),
        "revalidation_processed": len(due_revalidation_claims),
        "confirmed": confirmed,
        "conflicted": conflicted,
        "superseded": superseded,
        "pending": pending,
        "staled": staled,
        "revalidated_healthy": revalidated_healthy,
    }
