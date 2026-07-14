"""Bounded lifecycle-authoritative archival for the scheduled steward hook."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memorymaster.core import lifecycle
from memorymaster.recall import qdrant_outbox
from memorymaster.stores._storage_shared import ConcurrentModificationError


def _eligible(claim, cutoff: datetime) -> bool:
    if claim.access_count != 0:
        return False
    try:
        created_at = datetime.fromisoformat(claim.created_at)
    except (TypeError, ValueError):
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at < cutoff


def _schedule_vector_delete(service, claim) -> None:
    if service.qdrant is not None:
        service._qdrant_sync(claim)
        return
    db_path = getattr(service.store, "db_path", None)
    if db_path is not None:
        qdrant_outbox.enqueue(db_path, "delete", claim.id, None)


def run(service, *, older_than_days: int = 14, limit: int = 500) -> dict[str, int]:
    """Archive a bounded stale/unused batch through canonical transitions."""
    if older_than_days < 0 or limit <= 0:
        raise ValueError("older_than_days must be non-negative and limit must be positive")
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    stale = service.store.find_by_status("stale", limit=limit, include_citations=False)
    matched = [claim for claim in stale if _eligible(claim, cutoff)]
    archived = 0
    skipped = 0
    for claim in matched:
        try:
            updated = lifecycle.transition_claim(
                service.store,
                claim.id,
                "archived",
                reason=f"scheduled stale/unused archival after {older_than_days} days",
                event_type="staleness",
            )
        except (ConcurrentModificationError, ValueError):
            skipped += 1
            continue
        _schedule_vector_delete(service, updated)
        archived += 1
    return {"matched": len(matched), "archived": archived, "skipped": skipped}
