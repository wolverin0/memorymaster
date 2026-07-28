"""Preserve verifiable legacy event DAGs while converging the tenant ledger."""
from __future__ import annotations

import importlib
import logging
from typing import Any


VERSION = 18
DESCRIPTION = "Converge tenant ledger for verifiable legacy event DAGs"

logger = logging.getLogger(__name__)


def _v10():
    return importlib.import_module(
        "memorymaster.stores.migrations.0010_tenant_event_ledger"
    )


def _verify_primary_dag(rows: list[Any]) -> dict[str, int]:
    migration = _v10()
    hashes: dict[str, int] = {}
    predecessor_counts: dict[str, int] = {}
    predecessors: list[tuple[int, str]] = []
    for row in rows:
        event_id = int(migration._row_value(row, "id", 0))
        algorithm = (
            migration._text(migration._row_value(row, "hash_algo", 10))
            or migration._EVENT_HASH_ALGO
        )
        stored_previous = migration._text(
            migration._row_value(row, "prev_event_hash", 8)
        )
        stored_hash = migration._text(migration._row_value(row, "event_hash", 9))
        if stored_hash is None:
            raise RuntimeError(f"Legacy event {event_id} has no primary hash.")
        expected = migration._compute_primary_event_hash(
            row,
            hash_algo=algorithm,
            previous=stored_previous,
            normalize_utc=False,
        )
        if stored_hash != expected:
            raise RuntimeError(f"Legacy event content hash mismatch at event {event_id}.")
        if stored_hash in hashes:
            raise RuntimeError(f"Duplicate primary event hash at event {event_id}.")
        if stored_previous is not None:
            predecessor_counts[stored_previous] = (
                predecessor_counts.get(stored_previous, 0) + 1
            )
            predecessors.append((event_id, stored_previous))
        hashes[stored_hash] = event_id
    missing_predecessors = 0
    for event_id, previous in predecessors:
        predecessor_id = hashes.get(previous)
        if predecessor_id is None:
            missing_predecessors += 1
        elif predecessor_id >= event_id:
            raise RuntimeError(f"Forward primary event link at event {event_id}.")
    return {
        "events": len(rows),
        "fork_points": sum(count > 1 for count in predecessor_counts.values()),
        "missing_predecessors": missing_predecessors,
    }


def _backfill_tenant_rows(conn: Any, migration: Any) -> None:
    has_claims = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims'"
    ).fetchone()
    if has_claims is not None:
        conn.execute(
            """
            UPDATE events
            SET tenant_id = (
                SELECT claims.tenant_id FROM claims WHERE claims.id = events.claim_id
            )
            WHERE claim_id IS NOT NULL AND tenant_id IS NULL
            """
        )
    updates = migration._tenant_hash_updates(
        conn.execute(migration._EVENT_ROWS_SQL).fetchall()
    )
    if updates:
        conn.executemany(
            """
            UPDATE events
            SET tenant_prev_event_hash = ?, tenant_event_hash = ?,
                tenant_hash_algo = ?
            WHERE id = ?
            """,
            updates,
        )


def _ensure_tenant_indexes(conn: Any) -> None:
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_events_tenant_id ON events(tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_events_tenant_hash "
        "ON events(tenant_id, tenant_event_hash)",
        "CREATE INDEX IF NOT EXISTS idx_events_tenant_head "
        "ON events(tenant_id, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_events_tenant_algo_head "
        "ON events(tenant_id, hash_algo, id DESC)",
    ):
        conn.execute(statement)


def apply_sqlite(conn: Any) -> None:
    migration = _v10()
    has_events = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'"
    ).fetchone()
    if has_events is None:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        migration._add_sqlite_columns(conn)
        conn.execute("DROP TRIGGER IF EXISTS trg_events_append_only_update")
        conn.execute("DROP TRIGGER IF EXISTS trg_events_append_only_delete")
        rows = conn.execute(migration._EVENT_ROWS_SQL).fetchall()
        report = _verify_primary_dag(rows)
        del rows
        _backfill_tenant_rows(conn, migration)
        _ensure_tenant_indexes(conn)
        for statement in migration._SQLITE_APPEND_TRIGGER_STATEMENTS:
            conn.execute(statement)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    logger.warning("legacy event DAG preserved without primary rehash: %s", report)


def apply_postgres(conn: Any) -> None:
    conn.commit()
