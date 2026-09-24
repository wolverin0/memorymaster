"""Bounded lifecycle-authoritative archival for the scheduled steward hook.

Archival is irreversible, so a claim is archived only when BOTH hold (review
F-20): it is eligible by SQL (stale, never accessed, unpinned, older than the
cutoff) AND the decisions ledger records a live S1 ``no_longer_useful``
judgment for it (``memorymaster.decisions.archive_gate``). No ledger or no
judgment archives nothing: the clock alone never retires memory. With no
ledger the job does not page the stale backlog at all (every count is 0).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memorymaster.core import lifecycle
from memorymaster.recall import qdrant_outbox
from memorymaster.stores._storage_shared import ConcurrentModificationError

_PAGE_SIZE = 500


def _eligible(claim, cutoff: datetime) -> bool:
    if claim.status != "stale" or claim.access_count != 0 or claim.pinned:
        return False
    try:
        created_at = datetime.fromisoformat(claim.created_at)
    except (TypeError, ValueError):
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at < cutoff


def _ledger_available() -> bool:
    try:
        from memorymaster.decisions.archive_gate import ledger_available
    except ImportError:  # pragma: no cover - gate ships with the decisions package
        return False
    return ledger_available()


def _judged(claims) -> set[int]:
    """Claim ids with a current S1 judgment; empty when the gate is unavailable."""
    try:
        from memorymaster.decisions.archive_gate import judged_no_longer_useful
    except ImportError:  # pragma: no cover - gate ships with the decisions package
        return set()
    return judged_no_longer_useful({claim.id: claim.updated_at for claim in claims})


def _schedule_vector_delete(service, claim) -> None:
    if service.qdrant is not None:
        service._qdrant_sync(claim)
        return
    db_path = getattr(service.store, "db_path", None)
    if db_path is not None:
        qdrant_outbox.enqueue(db_path, "delete", claim.id, None)


def run(service, *, older_than_days: int = 14, limit: int = 500) -> dict[str, int]:
    """Archive at most ``limit`` judged stale/unused claims through lifecycle."""
    if older_than_days < 0 or limit <= 0:
        raise ValueError("older_than_days must be non-negative and limit must be positive")
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    created_before = cutoff.replace(microsecond=0).isoformat()
    eligible = matched = archived = skipped = 0
    after_id = 0
    # No ledger means no judgment can exist: skip paging the stale backlog.
    while archived + skipped < limit and _ledger_available():
        page = service.store.find_archive_candidates(
            created_before=created_before, after_id=after_id, limit=_PAGE_SIZE,
        )
        if not page:
            break
        after_id = page[-1].id
        candidates = [claim for claim in page if _eligible(claim, cutoff)]
        eligible += len(candidates)
        judged = _judged(candidates)
        matched += sum(1 for claim in candidates if claim.id in judged)
        for claim in candidates:
            if claim.id not in judged:
                continue
            if archived + skipped >= limit:
                break
            current = service.store.get_claim(claim.id, include_citations=False)
            if current is None or not _eligible(current, cutoff):
                skipped += 1
                continue
            try:
                updated = lifecycle.transition_claim(
                    service.store,
                    claim.id,
                    "archived",
                    reason=(
                        f"scheduled stale/unused archival after {older_than_days} days "
                        "(S1 judged no_longer_useful)"
                    ),
                    event_type="staleness",
                )
            except (ConcurrentModificationError, ValueError):
                skipped += 1
                continue
            _schedule_vector_delete(service, updated)
            archived += 1
        if len(page) < _PAGE_SIZE:
            break
    return {
        "eligible": eligible,
        "matched": matched,
        "archived": archived,
        "skipped": skipped,
        "unjudged": eligible - matched,
    }
