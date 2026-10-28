"""Give claim content its own fingerprint, separate from the idempotency key.

Review F14 (T-1865): the ingest facade deduplicated identical text by looking up a
generated ``hash-...`` value in ``claims.idempotency_key``. A claim first ingested
under an explicit key was therefore invisible to a later unkeyed copy and duplicates
accumulated. ``content_fingerprint`` is a SHA-256 of the stored text, looked up under
the tenant / scope / visibility / private-owner identity (``claim_identity``).

What this touches: it adds one nullable column and one index, then fills the column
for every existing row (``UPDATE claims SET content_fingerprint``). Nothing else is
rewritten: ``text``, ``status``, ``updated_at`` and ``version`` are not touched, so
the decay clock and FTS stay as they are. The migration-29 change cursor does fire
once per row (a one-time project-projection refresh). A row a guard trigger rejects
is left NULL and keeps using the legacy ``hash-`` key lookup. The whole migration runs
in one transaction and is safe to retry (the column add is guarded and only NULL rows
are filled).
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from typing import Any

VERSION = 30
DESCRIPTION = "claims.content_fingerprint for key-independent content dedup"
# Additive, derived and nullable: a v29 database simply lacks the column, and the
# common-column merge in bridges/db_merge.py copies it only when both sides have it.
MERGE_COMPATIBLE = True

logger = logging.getLogger(__name__)


def _fingerprint(text: str) -> str:
    # Frozen copy of claim_identity.content_fingerprint: a migration must not drift
    # if the live helper is ever changed.
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()


def _columns(conn: Any, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _apply(conn: Any) -> None:
    columns = _columns(conn, "claims")
    if "content_fingerprint" not in columns:
        conn.execute("ALTER TABLE claims ADD COLUMN content_fingerprint TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_claims_content_fingerprint "
        "ON claims(tenant_id, scope, content_fingerprint)"
    )
    skipped = 0
    for row in conn.execute(
        "SELECT id, text FROM claims WHERE content_fingerprint IS NULL AND text IS NOT NULL"
    ).fetchall():
        try:
            conn.execute(
                "UPDATE claims SET content_fingerprint = ? WHERE id = ?",
                (_fingerprint(str(row[1])), row[0]),
            )
        except sqlite3.IntegrityError:  # a RAISE(ABORT) guard trigger rejected this legacy row
            skipped += 1
    if skipped:
        logger.warning("content_fingerprint backfill skipped %d row(s) rejected by guards", skipped)


def apply_sqlite(conn: Any) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'claims'").fetchone() is None:
        return  # bare migration tests; the baseline schema creates claims in a real database
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
    """Fail closed: every migration from 19 on is SQLite-only (PostgreSQL rollout deferred)."""
    del conn
    raise RuntimeError("migration 30 is SQLite-only; PostgreSQL rollout is deferred")
