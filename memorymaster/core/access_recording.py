"""Recall access-counter writes, with the failure made visible (R6).

``MemoryService._record_accesses`` used to call the store directly inside
``contextlib.suppress(Exception)`` — no else-branch, no counter, no log. The
inherited ``record_access`` / ``record_accesses_batch`` in
``stores/_storage_lifecycle.py`` issue ``?``-placeholder UPDATEs that psycopg
rejects outright, so on a Postgres deployment ``claims.access_count`` was
pinned at 0 for the life of the database and nothing said so.

**Why that compounds rather than merely degrades.** ``recompute_tiers`` reads
``access_count`` to bucket claims:

    access_count > 5 OR created < 7d  -> core
    access_count = 0 AND created > 90d -> peripheral

A permanently-zero counter does not make that phase a no-op. It makes it
*confidently wrong*: every claim older than 90 days is demoted to
``peripheral``, and ``_tier_bonus`` then feeds a -0.10 penalty into ranking for
the entire aging corpus. Fixing the ``recompute_tiers`` Postgres dialect (see
``postgres_store.recompute_tiers``) made this louder in effect and no louder in
signal — which is why the counter below matters as much as the dialect fix.

The write stays best-effort; recall must not break because telemetry failed.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
from typing import Any

from memorymaster.core import observability
from memorymaster.core.security import sanitize_persisted_text

logger = logging.getLogger(__name__)

ACCESS_WRITE_FAILED = "claim_access_write_failed_total"


def record_accesses(store: Any, claim_ids: list[int]) -> bool:
    """Bump ``access_count``/``last_accessed`` for recalled claims.

    Returns True when the write reached the store, False when there was
    nothing to write, no supported store method, or the write failed. A
    failure increments ``claim_access_write_failed_total`` labelled by store
    class, so "0 accesses" can no longer be read as "nobody recalled anything".
    """
    if not claim_ids:
        return False
    backend = type(store).__name__
    try:
        batch = getattr(store, "record_accesses_batch", None)
        if callable(batch):
            batch(claim_ids)
            return True
        single = getattr(store, "record_access", None)
        if not callable(single):
            return False
        for claim_id in claim_ids:
            single(claim_id)
        return True
    except Exception:
        observability.bump_counter(ACCESS_WRITE_FAILED, backend=backend)
        logger.warning(
            "recording access for %d claim(s) failed (backend=%s); access_count "
            "and tier recomputation are now stale",
            len(claim_ids),
            backend,
            exc_info=True,
        )
        return False


def spool_accesses(db_path: Any, claim_ids: list[int], query_text: str = "") -> None:
    """Append one aggregated ``recall`` envelope to the JSONL spool instead of writing the DB.

    ``jobs/spool_drain`` replays it through the same sinks the read-write path uses
    (``record_accesses_batch``, ``FeedbackTracker.record_retrieval``). Best-effort: a spool I/O
    failure must never break recall. Shared by ``MemoryService._spool_accesses`` (the read-only
    store) and ``recall.delivery.confirm_turn`` (claims confirmed delivered after emission, T-2275).
    """
    db_path = str(db_path or "")
    if not db_path or not claim_ids:
        return
    from memorymaster.core import spool

    safe_query, _ = sanitize_persisted_text(query_text or "")
    query_hash = hashlib.sha256(safe_query.encode("utf-8")).hexdigest()[:12] if safe_query else None
    with contextlib.suppress(Exception):
        spool.append(db_path, "recall", {"claim_ids": list(dict.fromkeys(claim_ids)),
                                         "query_hash": query_hash, "query_text": safe_query})
