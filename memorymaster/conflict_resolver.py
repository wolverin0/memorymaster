"""Automatic conflict resolution for claims with the same (subject, predicate, scope) tuple.

When two claims share the same subject and predicate but differ in object_value,
this module picks a winner using a deterministic priority chain:
  1. Higher confidence score wins
  2. More recent (fresher updated_at) wins if confidence is equal
  3. More citations wins if both are equal
  4. Pinned claims always win over unpinned
  5. Optional LLM tiebreaker (if llm_steward is available)

The loser is transitioned to 'superseded' with a full audit trail.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from memorymaster.lifecycle import can_transition
from memorymaster.models import Claim

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ConflictPair:
    """Two claims that share the same tuple key but differ in object_value."""

    winner: Claim
    loser: Claim
    reason: str
    key: tuple[str, str, str]  # (subject, predicate, scope)


@dataclass(slots=True)
class ResolutionResult:
    """Summary of a conflict resolution run."""

    pairs_detected: int = 0
    pairs_resolved: int = 0
    pairs_skipped: int = 0
    resolutions: list[dict[str, Any]] = field(default_factory=list)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _citation_count(claim: Claim) -> int:
    return len(claim.citations) if claim.citations else 0


def _pick_winner(a: Claim, b: Claim) -> ConflictPair:
    """Deterministic resolution: pick the winner between two conflicting claims.

    Priority chain:
      1. Pinned claim wins over unpinned
      2. Higher confidence wins
      3. More recent updated_at wins
      4. More citations wins
      5. Higher claim id wins (deterministic tiebreaker)
    """
    key = (a.subject or "", a.predicate or "", a.scope)

    # Pinned always wins
    if a.pinned and not b.pinned:
        return ConflictPair(winner=a, loser=b, reason="pinned_wins", key=key)
    if b.pinned and not a.pinned:
        return ConflictPair(winner=b, loser=a, reason="pinned_wins", key=key)

    # Higher confidence
    if a.confidence > b.confidence:
        return ConflictPair(winner=a, loser=b, reason="higher_confidence", key=key)
    if b.confidence > a.confidence:
        return ConflictPair(winner=b, loser=a, reason="higher_confidence", key=key)

    # More recent
    a_ts = _parse_iso(a.updated_at)
    b_ts = _parse_iso(b.updated_at)
    if a_ts and b_ts:
        if a_ts > b_ts:
            return ConflictPair(winner=a, loser=b, reason="more_recent", key=key)
        if b_ts > a_ts:
            return ConflictPair(winner=b, loser=a, reason="more_recent", key=key)

    # More citations
    a_cites = _citation_count(a)
    b_cites = _citation_count(b)
    if a_cites > b_cites:
        return ConflictPair(winner=a, loser=b, reason="more_citations", key=key)
    if b_cites > a_cites:
        return ConflictPair(winner=b, loser=a, reason="more_citations", key=key)

    # Deterministic tiebreaker: higher id wins (most recently created)
    if a.id > b.id:
        return ConflictPair(winner=a, loser=b, reason="higher_id_tiebreaker", key=key)
    return ConflictPair(winner=b, loser=a, reason="higher_id_tiebreaker", key=key)


def _normalize_value(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip().lower()


def _build_conflict_groups(claims: list[Claim]) -> dict[tuple[str, str, str], list[Claim]]:
    """Group claims by (subject, predicate, scope) tuple, keeping only groups
    where claims disagree on object_value.
    """
    index: dict[tuple[str, str, str], list[Claim]] = {}
    for claim in claims:
        if not claim.subject or not claim.predicate:
            continue
        key = (claim.subject, claim.predicate, claim.scope)
        index.setdefault(key, []).append(claim)

    # Filter to groups with actual conflicts (different object_value)
    conflict_groups: dict[tuple[str, str, str], list[Claim]] = {}
    for key, group in index.items():
        if len(group) < 2:
            continue
        values = {_normalize_value(c.object_value) for c in group}
        if len(values) > 1:
            conflict_groups[key] = group

    return conflict_groups


def detect_conflicts(
    store: Any,
    *,
    limit: int = 500,
    statuses: list[str] | None = None,
) -> list[ConflictPair]:
    """Detect conflicting claims from the store.

    Returns a list of ConflictPair objects, one per conflicting pair.
    Only the highest-priority loser per group is returned (pairwise resolution).
    """
    if statuses is None:
        statuses = ["confirmed", "candidate", "conflicted"]

    claims = store.list_claims(
        status_in=statuses,
        limit=limit,
        include_archived=False,
        include_citations=True,
    )

    groups = _build_conflict_groups(claims)
    pairs: list[ConflictPair] = []

    for _key, group in groups.items():
        # Sort by confidence desc, then freshness desc, then id desc
        sorted_group = sorted(
            group,
            key=lambda c: (c.confidence, _parse_iso(c.updated_at) or datetime.min.replace(tzinfo=timezone.utc), c.id),
            reverse=True,
        )
        # Pairwise resolution: best claim is the winner, all others are losers
        best = sorted_group[0]
        for other in sorted_group[1:]:
            pair = _pick_winner(best, other)
            # Update best if this pair's winner is different (shouldn't happen with sorted order,
            # but _pick_winner has pinned logic that can override)
            best = pair.winner
            pairs.append(pair)

    return pairs


def resolve_conflicts(
    service: Any,
    *,
    dry_run: bool = False,
    limit: int = 500,
    statuses: list[str] | None = None,
) -> ResolutionResult:
    """Detect and resolve all conflicting claims.

    Args:
        service: MemoryService instance
        dry_run: If True, detect but don't apply transitions
        limit: Max claims to scan
        statuses: Which statuses to scan (default: confirmed, candidate, conflicted)

    Returns:
        ResolutionResult with details of all detected/resolved pairs.
    """
    pairs = detect_conflicts(service.store, limit=limit, statuses=statuses)
    result = ResolutionResult(pairs_detected=len(pairs))

    for pair in pairs:
        resolution_record = {
            "winner_id": pair.winner.id,
            "loser_id": pair.loser.id,
            "reason": pair.reason,
            "key": list(pair.key),
            "winner_confidence": pair.winner.confidence,
            "loser_confidence": pair.loser.confidence,
            "winner_object_value": pair.winner.object_value,
            "loser_object_value": pair.loser.object_value,
            "applied": False,
        }

        if dry_run:
            result.pairs_skipped += 1
            result.resolutions.append(resolution_record)
            continue

        # Skip if loser is already superseded or archived
        if pair.loser.status in ("superseded", "archived"):
            result.pairs_skipped += 1
            resolution_record["skip_reason"] = f"loser already {pair.loser.status}"
            result.resolutions.append(resolution_record)
            continue

        # Skip if loser is pinned (pinned claims should not be auto-superseded)
        if pair.loser.pinned:
            result.pairs_skipped += 1
            resolution_record["skip_reason"] = "loser is pinned"
            result.resolutions.append(resolution_record)
            continue

        # Check if transition is valid
        if not can_transition(pair.loser.status, "superseded"):
            result.pairs_skipped += 1
            resolution_record["skip_reason"] = f"cannot transition {pair.loser.status} -> superseded"
            result.resolutions.append(resolution_record)
            continue

        try:
            # Record the resolution decision as an audit event
            service.store.record_event(
                claim_id=pair.loser.id,
                event_type="policy_decision",
                from_status=pair.loser.status,
                to_status="superseded",
                details="conflict_auto_resolution",
                payload={
                    "source": "conflict_resolver",
                    "winner_id": pair.winner.id,
                    "loser_id": pair.loser.id,
                    "reason": pair.reason,
                    "winner_confidence": pair.winner.confidence,
                    "loser_confidence": pair.loser.confidence,
                },
            )

            # Transition the loser to superseded
            service.store.mark_superseded(
                old_claim_id=pair.loser.id,
                new_claim_id=pair.winner.id,
                reason=f"conflict_auto_resolution:{pair.reason}",
            )

            # Add a 'contradicts' link if the store supports it
            if hasattr(service.store, "add_claim_link"):
                # Link already exists or other non-critical error
                with contextlib.suppress(Exception):
                    service.store.add_claim_link(pair.winner.id, pair.loser.id, "contradicts")

            resolution_record["applied"] = True
            result.pairs_resolved += 1
        except Exception as exc:
            logger.warning(
                "Failed to resolve conflict: winner=%d loser=%d error=%s",
                pair.winner.id,
                pair.loser.id,
                exc,
            )
            result.pairs_skipped += 1
            resolution_record["skip_reason"] = str(exc)

        result.resolutions.append(resolution_record)

    return result
