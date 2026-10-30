"""Give decay its own clock, separate from ``updated_at``.

T-1870 AC3: decay aged a confirmed claim from ``updated_at``, and every confidence write
moves ``updated_at``. The cycle runs deterministic and the validator before decay, so a
real value change from either (no new evidence) restarted the decay clock. Two nullable
columns now hold that clock:

- ``last_evidence_at``: when evidence last arrived (ingest, or a confirmation).
- ``last_decayed_at``: when decay last charged the claim.

Decay ages from the later of the two; other writes no longer move it.

Backfill (only NULL cells, so a retry is safe):

- ``last_evidence_at`` = the latest of ``created_at``, the newest citation's ``created_at``
  and, for a confirmed claim, ``last_validated_at`` (its confirmation time; for other
  statuses that column also moves on stale/conflicted transitions, which are not evidence).
- ``last_decayed_at`` = ``updated_at`` for confirmed claims only. Decay has charged a
  confirmed claim at least up to there (or the interval was lost to an old rejuvenation),
  so the first run after this migration charges exactly what it would have charged before:
  no time is charged twice and nothing goes stale in a burst. Time lost before the
  migration is not reclaimed.

What this touches: two added columns and those two cells. ``text``, ``status``,
``confidence``, ``updated_at`` and ``version`` are not touched, so FTS and the corpus
generation stay as they are. A row a guard trigger rejects stays NULL; decay then falls
back to ``updated_at`` for it, as before. One transaction.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

VERSION = 32
DESCRIPTION = "claims.last_evidence_at / last_decayed_at: decay clock separate from updated_at"
# Additive and nullable: a v31 database simply lacks the columns, and the common-column
# merge in bridges/db_merge.py copies them only when both sides have them.
MERGE_COMPATIBLE = True

logger = logging.getLogger(__name__)


def _columns(conn: Any, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _latest(*values: Any) -> str | None:
    present = [str(v) for v in values if v]
    return max(present) if present else None


def _apply(conn: Any) -> None:
    columns = _columns(conn, "claims")
    for name in ("last_evidence_at", "last_decayed_at"):
        if name not in columns:
            conn.execute(f"ALTER TABLE claims ADD COLUMN {name} TEXT")
    skipped = 0
    rows = conn.execute(
        """
        SELECT c.id, c.status, c.created_at, c.updated_at, c.last_validated_at,
               c.last_evidence_at, c.last_decayed_at,
               (SELECT MAX(ci.created_at) FROM citations ci WHERE ci.claim_id = c.id) AS cited_at
        FROM claims c
        WHERE c.last_evidence_at IS NULL
           OR (c.last_decayed_at IS NULL AND c.status = 'confirmed')
        """
    ).fetchall()
    for row in rows:
        claim_id, status, created_at, updated_at, validated_at, evidence_at, decayed_at, cited_at = row
        confirmed = status == "confirmed"
        evidence = evidence_at or _latest(created_at, cited_at, validated_at if confirmed else None)
        decayed = decayed_at or (updated_at if confirmed else None)
        try:
            conn.execute(
                "UPDATE claims SET last_evidence_at = ?, last_decayed_at = ? WHERE id = ?",
                (evidence, decayed, claim_id),
            )
        except sqlite3.IntegrityError:  # a RAISE(ABORT) guard trigger rejected this legacy row
            skipped += 1
    if skipped:
        logger.warning("decay clock backfill skipped %d row(s) rejected by guards", skipped)


def apply_sqlite(conn: Any) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'claims'").fetchone() is None:
        return  # bare migration tests; the baseline schema creates claims in a real database
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'citations'").fetchone() is None:
        return
    owned = not conn.in_transaction
    if owned:
        conn.execute("BEGIN IMMEDIATE")
    try:
        _apply(conn)
    except BaseException:
        if owned:
            conn.rollback()
        raise
    if owned:
        conn.commit()


def apply_postgres(conn: Any) -> None:
    """Fail closed: every migration from 19 on is SQLite-only (PostgreSQL rollout deferred).

    ``schema_postgres.sql`` adds both columns (``ADD COLUMN IF NOT EXISTS``); NULL cells
    keep the legacy ``updated_at`` decay anchor there.
    """
    del conn
    raise RuntimeError("migration 32 is SQLite-only; PostgreSQL rollout is deferred")
