"""Persist the canonical (subject, predicate) identity of a claim.

Review F6 (T-1877): Python compares tuples through ``normalize_identity_part`` (Unicode
lower case, whitespace runs collapsed) but the storage lookup used SQLite
``lower(trim(...))`` (ASCII only, no collapsing). ``API  Gateway`` and ``api gateway``,
``ÁPI`` and ``ápi``, were equal to the semantic probe, which then skipped the pair, and
distinct to the deterministic lookup, so neither path saw the contradiction.
``claims.tuple_key`` holds the one Python-computed identity and the lookups compare it.

What this touches: it adds one nullable column and one index, then fills the column for
every existing row that has a subject and a predicate. Nothing else is rewritten:
``text``, ``status``, ``updated_at`` and ``version`` are not touched, so the decay clock
and FTS stay as they are. A row a guard trigger rejects, or one written later by a raw-SQL
path that does not set the key, stays NULL and keeps the legacy ASCII match in the lookup.
The migration runs in one transaction and is safe to retry (the column add is guarded and
only NULL rows are filled).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

VERSION = 31
DESCRIPTION = "claims.tuple_key: persisted canonical subject/predicate identity"
# Additive, derived and nullable: a v30 database simply lacks the column, and the
# common-column merge in bridges/db_merge.py copies it only when both sides have it.
MERGE_COMPATIBLE = True

logger = logging.getLogger(__name__)


def _part(value: Any) -> str:
    # Frozen copy of core.models.normalize_identity_part: a migration must not drift
    # if the live helper is ever changed.
    return " ".join((value or "").split()).lower()


def _key(subject: Any, predicate: Any) -> str | None:
    s, p = _part(subject), _part(predicate)
    return f"{s}\x1f{p}" if s and p else None


def _columns(conn: Any, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _apply(conn: Any) -> None:
    if "tuple_key" not in _columns(conn, "claims"):
        conn.execute("ALTER TABLE claims ADD COLUMN tuple_key TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_claims_tuple_key ON claims(tenant_id, scope, tuple_key)"
    )
    skipped = 0
    for row in conn.execute(
        "SELECT id, subject, predicate FROM claims "
        "WHERE tuple_key IS NULL AND subject IS NOT NULL AND predicate IS NOT NULL"
    ).fetchall():
        key = _key(row[1], row[2])
        if key is None:
            continue
        try:
            conn.execute("UPDATE claims SET tuple_key = ? WHERE id = ?", (key, row[0]))
        except sqlite3.IntegrityError:  # a RAISE(ABORT) guard trigger rejected this legacy row
            skipped += 1
    if skipped:
        logger.warning("tuple_key backfill skipped %d row(s) rejected by guards", skipped)


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
    raise RuntimeError("migration 31 is SQLite-only; PostgreSQL rollout is deferred")
