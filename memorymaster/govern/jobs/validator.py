from __future__ import annotations

import contextlib
import sqlite3

from memorymaster.core.config import get_config
from memorymaster.core.lifecycle import is_tuple_predecessor, transition_claim
from memorymaster.core.models import Claim, normalize_identity_part
from memorymaster.core.promotion_policy import (
    PromotionPolicyBlocked,
    auto_promotion_blocked,
    structure_blocks_auto_promotion,
    non_auto_promotable_sources,
)
from memorymaster.govern.steward_classifier import load_classifier, predict_promote_probability
from memorymaster.stores._storage_shared import ConcurrentModificationError

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


def _pending_supersession(store) -> frozenset[int]:
    """Claims with a supersession proposed but not yet resolved.

    WHY THE VALIDATOR CARES: declaring ``supersedes_claim_id`` does not retire
    the old claim, it files a `steward_proposal:superseded_candidate` for
    review. The validator never read that proposal, so it went on scoring the
    claim on its own evidence and promoting it to `confirmed` -- a claim
    somebody had explicitly declared outdated coming back stamped as verified
    truth. 21 of 59 production proposals were followed by exactly that
    promotion; claim 130542 sat at `confirmed` for ~11 hours after being
    declared superseded.

    ``use_cache=False``: the recall path tolerates a five-minute-old set
    because it only shifts ranking, but promotion is a state change that
    outlives the cycle. A proposal filed a minute ago must block this run.

    Never raises (the helper swallows and logs); an empty set means "promote as
    before", which is the pre-existing behaviour.
    """
    from memorymaster.recall.retrieval import pending_supersession_ids

    return pending_supersession_ids(store, use_cache=False)


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


_SOURCE_POLICY_HOLD = "promotion_blocked_source_policy"
_STRUCTURE_POLICY_HOLD = "promotion_blocked_structure_policy"


def _record_source_policy_hold(
    store, claim: Claim, score: float, citation_count: int, hold: str = _SOURCE_POLICY_HOLD
) -> None:
    """Audit the hold once, not on every cycle the claim stays a candidate."""
    latest = store.list_events(claim_id=claim.id, event_type="validator", limit=1)
    if latest and latest[0].details == hold:
        return
    store.record_event(
        claim_id=claim.id,
        event_type="validator",
        details=hold,
        payload={"score": score, "citation_count": citation_count, "claim_status": claim.status},
    )


def _retire_duplicate(store, claim: Claim, duplicate: Claim) -> str:
    reason = f"duplicate_of_confirmed_claim:{duplicate.id}"
    if duplicate.supersedes_claim_id in {None, claim.id}:
        store.mark_superseded(
            old_claim_id=claim.id,
            new_claim_id=duplicate.id,
            reason=reason,
        )
        return "superseded"
    transition_claim(
        store,
        claim_id=claim.id,
        to_status="archived",
        reason=reason,
        event_type="validator",
        replaced_by_claim_id=duplicate.id,
    )
    return "archived"


def _review_observations(store, limit: int) -> dict[str, int]:
    """Run the deterministic observation gate; never fall through to classifier."""
    try:
        from memorymaster.knowledge.graph_observation_engine import (
            review_observation_candidates,
        )

        result = review_observation_candidates(store, limit=limit)
        return {**result, "errors": 0}
    except Exception:  # noqa: BLE001 - observation promotion fails closed
        return {
            "checked": 0,
            "confirmed": 0,
            "archived": 0,
            "would_confirm": 0,
            "errors": 1,
        }


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
            "archived_duplicates": 0,
            "pending": 0,
            "skill_pending_approval": 0,
            "blocked_pending_supersession": 0,
            "held_by_source_policy": 0,
            "held_by_structure_policy": 0,
            "staled": 0,
            "revalidated_healthy": 0,
            "observation_checked": 0,
            "observation_confirmed": 0,
            "observation_archived": 0,
            "observation_errors": 0,
        }
    observation_review = _review_observations(store, limit)
    cfg = get_config()
    if min_score is None:
        min_score = cfg.validation_threshold
    candidate_claims = sorted(
        (
            claim
            # Held authors are excluded at fetch time: they would otherwise fill
            # the confidence-ordered window and starve every other candidate.
            for claim in store.find_by_status(
                "candidate", limit=limit, exclude_source_agents=non_auto_promotable_sources()
            )
            if (claim.claim_type or "").strip().lower() != "observation"
        ),
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
    pending_supersession = _pending_supersession(store)
    confirmed = 0
    blocked_pending_supersession = 0
    held_by_source_policy = 0
    held_by_structure_policy = 0
    conflicted = 0
    superseded = 0
    archived_duplicates = 0
    pending = 0
    skill_pending_approval = 0
    staled = 0
    revalidated_healthy = 0

    # Batch-fetch citation counts to avoid N+1 queries
    citation_counts = (
        store.count_citations_batch([c.id for c in claims])
        if hasattr(store, "count_citations_batch")
        else {c.id: store.count_citations(c.id) for c in claims}
    )

    for claim in claims:
        if claim.source_agent == "dream-worker" or (claim.idempotency_key or "").startswith("dream-"):
            from memorymaster.dreaming.source_review import review_allows_confirmation

            if not review_allows_confirmation(store, claim.id):
                pending += 1
                store.record_event(claim_id=claim.id, event_type="validator", details="dream_source_review_pending")
                continue
        is_revalidation = claim.status in {"confirmed", "stale", "conflicted"}
        citation_count = citation_counts.get(claim.id, 0)
        if claim.status == "candidate" and (claim.claim_type or "").strip().lower() == "skill":
            store.record_event(
                claim_id=claim.id,
                event_type="validator",
                details="skill_requires_explicit_approval",
                payload={"score": claim.confidence, "citation_count": citation_count},
            )
            pending += 1
            skill_pending_approval += 1
            continue
        score = validation_score(claim, citation_count, prior_confidence=claim.confidence)
        store.set_confidence(claim.id, score, details=f"validator_score={score:.3f};citations={citation_count}")

        if claim.id in pending_supersession:
            # Somebody declared this claim outdated and the steward has not
            # ruled yet. Promoting it now would confirm a claim that is queued
            # for retirement; leave its status exactly as it is until the
            # proposal is resolved either way.
            store.record_event(
                claim_id=claim.id,
                event_type="validator",
                details="promotion_blocked_pending_supersession",
                payload={"score": score, "citation_count": citation_count, "claim_status": claim.status},
            )
            pending += 1
            blocked_pending_supersession += 1
            continue

        related = store.find_confirmed_by_tuple(
            subject=claim.subject,
            predicate=claim.predicate,
            scope=claim.scope,
            exclude_claim_id=claim.id,
            tenant_id=claim.tenant_id,
            visibility=claim.visibility,
            source_agent=claim.source_agent,
        )
        # The lifecycle retires an explicit predecessor atomically with the
        # confirmation. It is neither a duplicate nor an unresolved rival.
        predecessor = next((x for x in related if is_tuple_predecessor(claim, x)), None)
        related = [x for x in related if not is_tuple_predecessor(claim, x)]

        claim_object = normalize_identity_part(claim.object_value)
        duplicate = next(
            (x for x in related if claim_object and normalize_identity_part(x.object_value) == claim_object),
            None,
        )
        if duplicate is not None and not is_revalidation:
            outcome = _retire_duplicate(store, claim, duplicate)
            superseded += int(outcome == "superseded")
            archived_duplicates += int(outcome == "archived")
            continue

        # Source policy (T-1354): some authors never reach confirmed through an
        # automatic path. Held before the confirm try/except below, which would
        # otherwise read the refusal as a tuple conflict and mark it conflicted.
        if claim.status != "confirmed" and auto_promotion_blocked(claim.source_agent):
            _record_source_policy_hold(store, claim, score, citation_count)
            pending += 1
            held_by_source_policy += 1
            continue

        # Structure policy (T-1591): a predicate without a subject is a shifted
        # field, not a triple; it is never confirmed automatically.
        if claim.status != "confirmed" and structure_blocks_auto_promotion(
            claim.subject, claim.predicate
        ):
            _record_source_policy_hold(store, claim, score, citation_count, _STRUCTURE_POLICY_HOLD)
            pending += 1
            held_by_structure_policy += 1
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

        # T-1863 F6: a rival whose object differs OR is missing on either side
        # is a conflict. Two missing objects are "unknown", never "same value".
        conflict = next(
            (x for x in related if not claim_object or normalize_identity_part(x.object_value) != claim_object),
            None,
        )
        # T-1877 (review F5): a score is never evidence of truth. A rival with another value stays untouched and
        # the newcomer is conflicted; only an explicit supersession link (the `predecessor` above) retires a
        # rival, the same rule `curation_drain._verdict` applies.
        if conflict is not None:
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
            retirement = predecessor
            try:
                transition_claim(
                    store,
                    claim_id=claim.id,
                    to_status="confirmed",
                    reason=f"validated score={score:.3f} citations={citation_count}",
                    event_type="validator",
                    **({"supersedes_claim_id": retirement.id} if retirement is not None else {}),
                )
                confirmed += 1
                superseded += int(retirement is not None)
            except PromotionPolicyBlocked:
                raise
            except (sqlite3.IntegrityError, ValueError, ConcurrentModificationError) as exc:
                if isinstance(exc, sqlite3.IntegrityError):
                    # A concurrent confirmation can take the unique tuple slot.
                    if not any(name in str(exc) for name in (
                        "idx_claims_public_confirmed_tuple_unique",
                        "idx_claims_nonpublic_principal_confirmed_tuple_unique",
                    )) or not store.find_confirmed_by_tuple(
                        subject=claim.subject, predicate=claim.predicate, scope=claim.scope,
                        exclude_claim_id=claim.id, tenant_id=claim.tenant_id,
                        visibility=claim.visibility, source_agent=claim.source_agent,
                    ):
                        raise
                elif retirement is None or (
                    isinstance(exc, ValueError)
                    and str(exc) != "Supersession requires a current unpinned tuple predecessor."
                ):
                    # Only the atomic predecessor refusal is an unresolved tuple.
                    # Source-review and unrelated storage failures still propagate.
                    raise
                if claim.status != "conflicted":
                    transition_claim(
                        store,
                        claim_id=claim.id,
                        to_status="conflicted",
                        reason=f"tuple_conflict_on_confirm score={score:.3f}",
                        event_type="validator",
                    )
                conflicted += 1
                continue
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

    return {
        "processed": len(claims),
        "candidate_processed": len(candidate_claims),
        "revalidation_processed": len(due_revalidation_claims),
        "confirmed": confirmed,
        "conflicted": conflicted,
        "superseded": superseded,
        "archived_duplicates": archived_duplicates,
        "pending": pending,
        "skill_pending_approval": skill_pending_approval,
        "blocked_pending_supersession": blocked_pending_supersession,
        "held_by_source_policy": held_by_source_policy,
        "held_by_structure_policy": held_by_structure_policy,
        "staled": staled,
        "revalidated_healthy": revalidated_healthy,
        "observation_checked": observation_review["checked"],
        "observation_confirmed": observation_review["confirmed"],
        "observation_archived": observation_review["archived"],
        "observation_errors": observation_review["errors"],
    }
