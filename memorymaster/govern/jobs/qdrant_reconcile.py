"""Qdrant reconciliation steward phase (P1 WAL-discipline spec §2.7).

Qdrant sync is fire-and-forget (spec F11): ``_qdrant_sync`` swallows
exceptions and ``_qdrant_post_cycle_sync`` only re-upserts recently-changed
claims, so the vector index silently drifts from SQLite truth — missed
upserts accumulate, and points for archived/deleted claims linger forever.
This job closes the loop, wired as a throttled (1/day) phase at the end of
``service.run_cycle`` and exposed via the ``qdrant-reconcile`` CLI command:

1. **Drift metric**: SQLite truth count (claims in the statuses ``sync_all``
   pushes) vs the exact Qdrant point count; recorded as a ``qdrant_drift``
   ``system`` event so the §2.10 dashboard can plot it.
2. **Convergence on breach**: if ``|drift|`` exceeds the threshold
   (``MEMORYMASTER_QDRANT_DRIFT_MAX``, default 100) or the operator passes
   ``--full``, delete points whose claim is archived/missing, then run
   ``QdrantBackend.sync_all(store)`` to upsert what is missing.
3. **Clean skip** when no backend is configured (``QDRANT_URL`` unset →
   ``service.qdrant is None``) or Qdrant is unreachable — the phase must
   never fail the surrounding cycle on a machine without Qdrant.

The throttle stamp is the ``qdrant_drift`` event itself (MAX(created_at) per
marker — same append-only mechanism as ``jobs/integrity.py``), recorded only
after a successful reconcile so an unreachable backend does not consume the
daily slot.
"""
from __future__ import annotations

import logging
import os
from contextlib import closing
from datetime import datetime
from pathlib import Path

from memorymaster.stores._storage_shared import connect_ro
from memorymaster.govern.jobs.integrity import _due, _record

logger = logging.getLogger(__name__)

ENV_DRIFT_MAX = "MEMORYMASTER_QDRANT_DRIFT_MAX"
DEFAULT_DRIFT_MAX = 100

# Throttle/metric marker recorded as a `system` event (see jobs/integrity.py).
MARKER_DRIFT = "qdrant_drift"
RECONCILE_INTERVAL_HOURS = 24

# Statuses sync_all pushes (qdrant_backend.py:sync_all). The SQLite truth set
# MUST mirror it exactly or the drift metric reports phantom drift forever.
SYNC_STATUSES = ("confirmed", "stale", "candidate", "conflicted")


def drift_threshold() -> int:
    """Threshold from MEMORYMASTER_QDRANT_DRIFT_MAX; default 100 (spec §2.7)."""
    raw = os.environ.get(ENV_DRIFT_MAX, "").strip()
    try:
        return int(raw) if raw else DEFAULT_DRIFT_MAX
    except ValueError:
        logger.warning("invalid %s=%r — using default %d", ENV_DRIFT_MAX, raw, DEFAULT_DRIFT_MAX)
        return DEFAULT_DRIFT_MAX


def _truth_conn(store, db_path: str | Path | None):
    """Read-only conn when we have a SQLite path; store conn otherwise."""
    if db_path:
        return closing(connect_ro(db_path))
    return closing(store.connect())


_STATUS_PLACEHOLDERS = ", ".join("?" for _ in SYNC_STATUSES)


def sqlite_truth_count(store, db_path: str | Path | None = None) -> int:
    """Count of claims in the statuses sync_all would push to Qdrant."""
    db_path = db_path or getattr(store, "db_path", None)
    with _truth_conn(store, db_path) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM claims WHERE status IN ({_STATUS_PLACEHOLDERS})",
            SYNC_STATUSES,
        ).fetchone()
    return int(row[0])


def _live_claim_ids(store, db_path: str | Path | None) -> set[int]:
    with _truth_conn(store, db_path) as conn:
        rows = conn.execute(
            f"SELECT id FROM claims WHERE status IN ({_STATUS_PLACEHOLDERS})",
            SYNC_STATUSES,
        ).fetchall()
    return {int(r[0]) for r in rows}


def _delete_orphan_points(store, qdrant, db_path: str | Path | None) -> int:
    """Delete Qdrant points whose claim is archived or missing in SQLite.

    This is the half of convergence sync_all cannot do — it only upserts.
    """
    point_claim_ids = qdrant.list_point_claim_ids()
    if point_claim_ids is None:
        return 0
    live = _live_claim_ids(store, db_path)
    deleted = 0
    for claim_id in point_claim_ids:
        if claim_id not in live and qdrant.delete_claim(claim_id):
            deleted += 1
    if deleted:
        logger.info("qdrant reconcile: deleted %d orphan points", deleted)
    return deleted


def run(
    store,
    qdrant,
    *,
    db_path: str | Path | None = None,
    now: datetime | None = None,
    force: bool = False,
    full: bool = False,
    threshold: int | None = None,
) -> dict[str, object]:
    """Reconcile SQLite truth vs Qdrant; sync_all + orphan delete on breach.

    Throttled to once per 24 h unless ``force`` (the operator CLI path).
    Never raises into the surrounding cycle — unreachable Qdrant is a skip,
    not an error.
    """
    if qdrant is None:
        return {"skipped": "no_qdrant"}
    db_path = db_path or getattr(store, "db_path", None)
    if not force and db_path is not None and not _due(
        db_path, MARKER_DRIFT, hours=RECONCILE_INTERVAL_HOURS, now=now
    ):
        return {"skipped": "throttled"}

    qdrant_count = qdrant.count_points()
    if qdrant_count is None:
        return {"skipped": "qdrant_unavailable"}
    try:
        sqlite_count = sqlite_truth_count(store, db_path)
    except Exception as exc:
        logger.warning("qdrant reconcile: truth count failed: %s", exc)
        return {"error": str(exc)}

    drift = abs(sqlite_count - qdrant_count)
    limit = threshold if threshold is not None else drift_threshold()
    result: dict[str, object] = {
        "sqlite_count": sqlite_count,
        "qdrant_count": qdrant_count,
        "drift": drift,
        "threshold": limit,
        "synced": False,
        "upserted": 0,
        "deleted": 0,
    }
    if full or drift > limit:
        result["deleted"] = _delete_orphan_points(store, qdrant, db_path)
        stats = qdrant.sync_all(store)
        result["synced"] = True
        result["upserted"] = int(stats.get("synced", 0))
        result["sync_stats"] = dict(stats)
        logger.info(
            "qdrant reconcile: drift=%d threshold=%d full=%s — sync_all ran: %s",
            drift, limit, full, stats,
        )
    _record(store, MARKER_DRIFT, {k: result[k] for k in (
        "sqlite_count", "qdrant_count", "drift", "threshold", "synced", "upserted", "deleted",
    )})
    return result
