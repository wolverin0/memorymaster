"""Automatic conflict resolution for claims with the same (subject, predicate, scope) tuple.

When two claims share the same subject and predicate but differ in object_value,
that is a contradiction, and (T-1863 F5 / T-1870) it is settled ONLY by an
explicit supersession link or an operator pin (``_verdict``). Confidence,
recency and citation counts (``_pick_winner``) pick a winner solely between
claims that assert the same non-empty value, i.e. duplicates. Any group that
cannot be settled that way stays untouched and is reported as held for review.

The loser of a settled pair is transitioned to 'superseded' with a full audit trail.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from memorymaster.stores._storage_shared import utc_now
from memorymaster.core.lifecycle import can_transition
from memorymaster.core.models import Claim, normalize_identity_part

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ConflictPair:
    """Two claims that share the same tuple key but differ in object_value."""

    winner: Claim
    loser: Claim
    reason: str
    key: tuple[str, str, str]  # (subject, predicate, scope)
    # True when the group has no explicit-link/pin resolution: winner/loser are
    # only a display anchor and nothing may be superseded.
    held: bool = False


@dataclass(slots=True)
class ResolutionResult:
    """Summary of a conflict resolution run."""

    pairs_detected: int = 0
    pairs_resolved: int = 0
    pairs_skipped: int = 0
    pairs_held: int = 0
    resolutions: list[dict[str, Any]] = field(default_factory=list)


class SupersessionRaceLost(RuntimeError):
    """Raised when another writer superseded the old claim first."""

    def __init__(self, old_claim_id: int, current_replacement_id: int | None) -> None:
        super().__init__(
            f"Claim {old_claim_id} was already superseded"
            + (f" by {current_replacement_id}." if current_replacement_id is not None else ".")
        )
        self.old_claim_id = old_claim_id
        self.current_replacement_id = current_replacement_id


def _supports_sqlite_atomic_supersede(store: Any) -> bool:
    return (
        store.__class__.__name__ == "SQLiteStore"
        and callable(getattr(store, "connect", None))
        and callable(getattr(store, "_insert_event_row", None))
    )


def supersede_claim(
    store: Any,
    *,
    old_claim_id: int,
    new_claim_id: int,
    reason: str,
    event_type: str = "supersession",
) -> Claim:
    """Atomically link both sides of a supersession, or report a lost race."""
    if _supports_sqlite_atomic_supersede(store):
        now = utc_now()
        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                old = conn.execute(
                    "SELECT id, status, replaced_by_claim_id FROM claims WHERE id = ?",
                    (old_claim_id,),
                ).fetchone()
                if old is None:
                    raise ValueError(f"Claim {old_claim_id} does not exist.")

                current_replacement_id = old["replaced_by_claim_id"]
                if old["status"] == "superseded" or current_replacement_id is not None:
                    raise SupersessionRaceLost(old_claim_id, current_replacement_id)
                if not can_transition(old["status"], "superseded"):
                    raise ValueError(f"Invalid transition: {old['status']} -> superseded")

                new = conn.execute("SELECT id FROM claims WHERE id = ?", (new_claim_id,)).fetchone()
                if new is None:
                    raise ValueError(f"Replacement claim {new_claim_id} does not exist.")

                conn.execute(
                    """
                    UPDATE claims
                    SET status = 'superseded',
                        replaced_by_claim_id = ?,
                        updated_at = ?,
                        version = version + 1,
                        valid_until = COALESCE(?, valid_until)
                    WHERE id = ?
                    """,
                    (new_claim_id, now, now, old_claim_id),
                )
                conn.execute(
                    """
                    UPDATE claims
                    SET supersedes_claim_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (old_claim_id, now, new_claim_id),
                )
                store._insert_event_row(
                    conn,
                    claim_id=old_claim_id,
                    event_type=event_type,
                    from_status=old["status"],
                    to_status="superseded",
                    details=reason,
                    payload_json=json.dumps({"replaced_by_claim_id": new_claim_id}),
                    created_at=now,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        updated = store.get_claim(old_claim_id)
        if updated is None:
            raise RuntimeError("Failed to load claim after supersession.")
        return updated

    current = store.get_claim(old_claim_id, include_citations=False)
    if current is None:
        raise ValueError(f"Claim {old_claim_id} does not exist.")
    if current.status == "superseded" or current.replaced_by_claim_id is not None:
        raise SupersessionRaceLost(old_claim_id, current.replaced_by_claim_id)
    store.mark_superseded(old_claim_id, new_claim_id, reason)
    updated = store.get_claim(old_claim_id, include_citations=False)
    if updated is None:
        raise RuntimeError("Failed to load claim after supersession.")
    if updated.replaced_by_claim_id != new_claim_id:
        raise SupersessionRaceLost(old_claim_id, updated.replaced_by_claim_id)
    return updated


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
    if a.pinned != b.pinned:
        return ConflictPair(
            winner=a if a.pinned else b,
            loser=b if a.pinned else a,
            reason="pinned_wins",
            key=key,
        )

    # Higher confidence
    if a.confidence != b.confidence:
        return ConflictPair(
            winner=a if a.confidence > b.confidence else b,
            loser=b if a.confidence > b.confidence else a,
            reason="higher_confidence",
            key=key,
        )

    # More recent
    a_ts = _parse_iso(a.updated_at)
    b_ts = _parse_iso(b.updated_at)
    if a_ts and b_ts and a_ts != b_ts:
        return ConflictPair(
            winner=a if a_ts > b_ts else b,
            loser=b if a_ts > b_ts else a,
            reason="more_recent",
            key=key,
        )

    # More citations
    a_cites = _citation_count(a)
    b_cites = _citation_count(b)
    if a_cites != b_cites:
        return ConflictPair(
            winner=a if a_cites > b_cites else b,
            loser=b if a_cites > b_cites else a,
            reason="more_citations",
            key=key,
        )

    # Deterministic tiebreaker: higher id wins (most recently created)
    return ConflictPair(
        winner=a if a.id > b.id else b,
        loser=b if a.id > b.id else a,
        reason="higher_id_tiebreaker",
        key=key,
    )


def _verdict(claim: Any, rival: Any) -> tuple[str, str] | None:
    """Who wins between a conflicted claim and its confirmed rival, or None.

    T-1863 F5: different-value contradictions are settled ONLY by an explicit
    supersession link (either direction) or by the operator's pin on the rival.
    Confidence / freshness / citations (``_pick_winner``) are used solely when
    both claims assert the same non-empty value, i.e. a duplicate rather than a
    contradiction. Missing objects count as unknown, not as "same".
    """
    if claim.supersedes_claim_id == rival.id:
        return "claim_wins", "supersedes_explicit"
    if rival.supersedes_claim_id == claim.id:
        return "claim_loses", "rival_supersedes_explicit"
    if rival.pinned and not claim.pinned:
        return "claim_loses", "pinned_wins"
    claim_object = normalize_identity_part(claim.object_value)
    if claim_object and claim_object == normalize_identity_part(rival.object_value):
        pair = _pick_winner(claim, rival)
        return ("claim_loses" if pair.loser.id == claim.id else "claim_wins"), pair.reason
    return None


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
        key = (normalize_identity_part(claim.subject), normalize_identity_part(claim.predicate), claim.scope)
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
    One pair per non-champion claim in a group; pairs of an unsettled group carry
    ``held=True`` (no explicit supersession link or pin) and must not be applied.
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

    for key, group in groups.items():
        # Sort by confidence desc, then freshness desc, then id desc
        sorted_group = sorted(
            group,
            key=lambda c: (c.confidence, _parse_iso(c.updated_at) or datetime.min.replace(tzinfo=timezone.utc), c.id),
            reverse=True,
        )
        champion_pairs = _settle_group(sorted_group)
        if champion_pairs is not None:
            pairs.extend(champion_pairs)
            continue
        # shortcut: the whole group is held if any claim lacks an explicit
        # link/pin against a single champion (a mixed group is never partially
        # settled); upgrade only if a real chain case (A->B->C links) needs it.
        anchor = sorted_group[0]
        for other in sorted_group[1:]:
            pair = _pick_winner(anchor, other)
            pair.held = True
            pairs.append(pair)

    return pairs


def _settle_group(sorted_group: list[Claim]) -> list[ConflictPair] | None:
    """Pairs (champion, loser) if ONE claim beats every other by ``_verdict``.

    Returns None when no such champion exists. A single champion keeps every
    ``replaced_by`` pointer on a claim that stays alive (no dead-link chains),
    and holding the whole group otherwise is the safer choice for a mixed group.
    """
    for champion in sorted_group:
        settled: list[ConflictPair] = []
        for other in sorted_group:
            if other is champion:
                continue
            decided = _verdict(other, champion)
            if decided is None or decided[0] != "claim_loses":
                break
            settled.append(
                ConflictPair(
                    winner=champion,
                    loser=other,
                    reason=decided[1],
                    key=(champion.subject or "", champion.predicate or "", champion.scope),
                )
            )
        else:
            return settled
    return None


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
            "held": pair.held,
        }

        if pair.held:
            # No explicit supersession link or pin: leave both claims alone.
            result.pairs_skipped += 1
            result.pairs_held += 1
            resolution_record["skip_reason"] = "held for review"
            result.resolutions.append(resolution_record)
            continue

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

            # Transition the loser to superseded and link both sides atomically.
            supersede_claim(
                service.store,
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
        except SupersessionRaceLost as exc:
            result.pairs_skipped += 1
            resolution_record["skip_reason"] = "lost_race"
            resolution_record["current_replacement_id"] = exc.current_replacement_id
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
