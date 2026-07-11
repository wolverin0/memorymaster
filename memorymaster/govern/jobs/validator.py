from __future__ import annotations

import contextlib

from memorymaster.core.config import get_config
from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import Claim
from memorymaster.govern.steward_classifier import load_classifier, predict_promote_probability

# Calibrated-classifier decision threshold — the (precision>=0.95, max recall)
# operating point from the Pareto sweep. See
# artifacts/spec-steward-classifier-2026-04-23.md.
_CLF_THRESHOLD = 0.65


def validation_score(claim: Claim, citation_count: int, prior_confidence: float) -> float:
    base = 0.35
    citation_bonus = min(citation_count * 0.12, 0.4)
    length_bonus = min(len(claim.text) / 240.0, 0.15)
    structure_bonus = 0.1 if claim.subject and claim.predicate and claim.object_value else 0.0
    raw = base + citation_bonus + length_bonus + structure_bonus
    blended = (raw * 0.75) + (prior_confidence * 0.25)
    return max(0.0, min(1.0, blended))


def _classifier_promote_decision(store, claim: Claim, citation_count: int) -> bool | None:
    """Run the calibrated classifier gate. Returns ``True``/``False`` to accept
    or reject promotion, or ``None`` when the artifact is unavailable (caller
    MUST fall back to the legacy additive formula).

    Never raises — any failure returns ``None`` so the validator stays alive.
    """
    clf = load_classifier()
    if clf is None:
        return None
    try:
        connect = getattr(store, "connect", None)
        if connect is None:
            return None
        with contextlib.closing(connect()) as conn:
            proba = predict_promote_probability(claim, conn, classifier=clf)
    except Exception:
        return None
    if proba is None:
        return None
    return proba >= _CLF_THRESHOLD and citation_count >= 1


def _merge_claims(primary: list[Claim], secondary: list[Claim]) -> list[Claim]:
    seen: set[int] = set()
    merged: list[Claim] = []
    for claim in primary + secondary:
        if claim.id in seen:
            continue
        seen.add(claim.id)
        merged.append(claim)
    return merged


def _candidate_recency_key(claim: Claim) -> tuple[str, int]:
    """Order candidates by immutable ingest recency, never mutable confidence writes."""
    return (claim.created_at, claim.id)


def run(
    store,
    limit: int = 200,
    min_citations: int = 1,
    min_score: float | None = None,
    revalidation_claims: list[Claim] | None = None,
    policy_mode: str = "legacy",
) -> dict[str, int]:
    # Promotion freeze (P1 spec §2.5.2): a failed quick_check writes the
    # <db>.integrity-failed sentinel; promoting/transitioning claims through
    # a DB with a broken btree compounds the damage, so the validator no-ops
    # until the operator clears the sentinel.
    from memorymaster.govern.jobs.integrity import promotions_frozen_for

    if promotions_frozen_for(store):
        return {
            "frozen": 1,
            "processed": 0,
            "candidate_processed": 0,
            "revalidation_processed": 0,
            "confirmed": 0,
            "conflicted": 0,
            "superseded": 0,
            "pending": 0,
            "staled": 0,
            "revalidated_healthy": 0,
        }
    cfg = get_config()
    if min_score is None:
        min_score = cfg.validation_threshold
    candidate_claims = sorted(
        store.find_by_status("candidate", limit=limit),
        key=_candidate_recency_key,
        reverse=True,
    )
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
            tenant_id=claim.tenant_id,
            visibility=claim.visibility,
            source_agent=claim.source_agent,
        )

        duplicate = next((x for x in related if x.object_value == claim.object_value and x.object_value), None)
        if duplicate is not None and not is_revalidation:
            store.mark_superseded(
                old_claim_id=claim.id,
                new_claim_id=duplicate.id,
                reason=f"duplicate_of_confirmed_claim:{duplicate.id}",
            )
            superseded += 1
            continue

        # Calibrated-classifier gate (task #129): when the artifact is present
        # AND its feature_version matches, use the learned probability; else
        # fall back to the legacy (min_citations, min_score) additive formula.
        clf_decision = _classifier_promote_decision(store, claim, citation_count)
        legacy_gate = citation_count < min_citations or score < min_score
        if clf_decision is not None:
            promote_blocked = not clf_decision
        else:
            promote_blocked = legacy_gate
        if promote_blocked:
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
            try:
                transition_claim(
                    store,
                    claim_id=claim.id,
                    to_status="confirmed",
                    reason=f"validated score={score:.3f} citations={citation_count}",
                    event_type="validator",
                )
                confirmed += 1
            except Exception:
                # Tuple uniqueness conflict — another confirmed claim has the same
                # (subject, predicate, scope). Mark as conflicted instead of crashing.
                if claim.status != "conflicted":
                    transition_claim(
                        store,
                        claim_id=claim.id,
                        to_status="conflicted",
                        reason=f"tuple_conflict_on_confirm score={score:.3f}",
                        event_type="validator",
                    )
                conflicted += 1
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
