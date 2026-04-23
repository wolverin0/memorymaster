"""Backfill citations for orphan ``llm-stop-hook`` claims.

Wave 2-F finding (#128): 821 of 824 candidates with zero citations were
created by the auto-ingest stop hook, which inserted directly into ``claims``
without ever inserting into ``citations``. The steward ``min_citations >= 1``
gate permanently rejects them.

The forward-fix lives in
``memorymaster-auto-ingest.py`` (both installed and template copies).
This script backfills the **existing** orphans so they become steward-eligible.

Usage::

    python scripts/backfill_stop_hook_citations.py --db memorymaster.db --dry-run
    python scripts/backfill_stop_hook_citations.py --db memorymaster.db --apply

Default is ``--dry-run``. ``--apply`` wraps every INSERT in a single
transaction; on any error the whole migration rolls back.

The synthesized citation uses ``source='llm-stop-hook-backfill'`` (distinct
from the live hook's ``'llm-stop-hook'``) so we can tell reconstructed rows
from real ones in audits.

Scope filter (``--source-agent``) defaults to ``llm-stop-hook`` so other
hooks' orphans are not touched. Pass e.g. ``--source-agent any`` to backfill
every orphan regardless of agent.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="memorymaster.db", help="Path to memorymaster.db (default: %(default)s)")
    p.add_argument("--source-agent", default="llm-stop-hook",
                   help="Only backfill claims with this source_agent. Pass 'any' to target all orphans. Default: %(default)s")
    p.add_argument("--apply", action="store_true", help="Write changes. Default is dry-run.")
    p.add_argument("--dry-run", action="store_true", help="Explicit dry-run (default behavior).")
    return p.parse_args()


def find_orphans(conn: sqlite3.Connection, source_agent: str) -> list[tuple[int, str, str]]:
    """Return (id, scope, text) for claims with no matching citation row."""
    sql = (
        "SELECT id, scope, text FROM claims "
        "WHERE id NOT IN (SELECT claim_id FROM citations WHERE claim_id IS NOT NULL)"
    )
    params: tuple = ()
    if source_agent != "any":
        sql += " AND source_agent = ?"
        params = (source_agent,)
    sql += " ORDER BY id"
    return list(conn.execute(sql, params).fetchall())


def backfill(conn: sqlite3.Connection, orphans: list[tuple[int, str, str]]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for claim_id, scope, text in orphans:
        conn.execute(
            "INSERT INTO citations (claim_id, source, locator, excerpt, created_at) "
            "VALUES (?, 'llm-stop-hook-backfill', ?, ?, ?)",
            (claim_id, scope or "project", (text or "")[:200], now),
        )
        inserted += 1
    return inserted


def main() -> int:
    args = parse_args()
    if args.apply and args.dry_run:
        print("ERROR: --apply and --dry-run are mutually exclusive", file=sys.stderr)
        return 2

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db_path)
    try:
        before = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE id NOT IN (SELECT claim_id FROM citations)"
        ).fetchone()[0]
        orphans = find_orphans(conn, args.source_agent)
        print(f"DB: {db_path}")
        print(f"Source-agent filter: {args.source_agent!r}")
        print(f"Total orphan claims (any source): {before}")
        print(f"Orphans matching filter:          {len(orphans)}")

        if not orphans:
            print("Nothing to backfill. Exiting.")
            return 0

        # Show a small sample so a human can sanity-check before --apply.
        print()
        print("Sample (first 5):")
        for claim_id, scope, text in orphans[:5]:
            excerpt = (text or "").replace("\n", " ")[:100]
            print(f"  id={claim_id:<6} scope={scope or '<null>':<32} {excerpt!r}")

        if not args.apply:
            print()
            print(f"DRY-RUN: would insert {len(orphans)} citation rows with source='llm-stop-hook-backfill'.")
            print("Re-run with --apply to write.")
            return 0

        print()
        print(f"Applying: inserting {len(orphans)} citation rows in a single transaction...")
        conn.execute("BEGIN")
        try:
            inserted = backfill(conn, orphans)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        after = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE id NOT IN (SELECT claim_id FROM citations)"
        ).fetchone()[0]
        print(f"Inserted: {inserted}")
        print(f"Orphan claims before: {before}")
        print(f"Orphan claims after:  {after}")
        print(f"Delta: {before - after}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
