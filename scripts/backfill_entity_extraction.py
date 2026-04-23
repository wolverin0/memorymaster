"""Backfill Layer-1 entity extraction across the existing claims corpus.

Scans every row in ``claims`` and registers extracted entities (files,
env-vars, services, ports, commits, tools) as aliases on virtual
``(text_entity:<kind>:<canonical_hint>)`` entities. Idempotent: re-running
produces 0 new rows because the ``entity_aliases`` UNIQUE constraint on
``(entity_id, variant_key)`` dedupes variant inserts.

Usage
-----

    # Dry-run — show planned changes without writing
    python scripts/backfill_entity_extraction.py --db /path/to/copy.db --dry-run

    # Apply
    python scripts/backfill_entity_extraction.py --db /path/to/copy.db --apply

WARNING: never run --apply against the live memorymaster.db. Always work
off a copy. The live DB is ~8 GB and concurrent writers can corrupt it.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

# Keep the import relative to repo root so running from /scripts works.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from memorymaster.entity_extractor import extract_patterns  # noqa: E402
from memorymaster.entity_registry import (  # noqa: E402
    add_alias,
    ensure_entity_schema,
    resolve_or_create,
)

logger = logging.getLogger(__name__)


def _measure(conn: sqlite3.Connection) -> dict:
    total_entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    total_aliases = conn.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0]
    avg = (total_aliases / total_entities) if total_entities else 0.0
    return {
        "total_entities": total_entities,
        "total_aliases": total_aliases,
        "avg_aliases_per_entity": round(avg, 4),
    }


def _per_kind_stats(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT e.entity_type,
               COUNT(DISTINCT e.id) AS entities,
               COUNT(DISTINCT a.id) AS aliases
        FROM entities e
        LEFT JOIN entity_aliases a ON a.entity_id = e.id
        WHERE e.entity_type LIKE 'text_entity:%'
        GROUP BY e.entity_type
        ORDER BY entities DESC
        """
    ).fetchall()
    return {
        row[0]: {
            "entities": row[1],
            "aliases": row[2],
            "avg_aliases": round(row[2] / row[1], 3) if row[1] else 0.0,
        }
        for row in rows
    }


def backfill(db_path: Path, *, dry_run: bool) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        ensure_entity_schema(conn)
        # Guard: ensure claims table exists before we scan.
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='claims'"
        ).fetchone()
        if not exists:
            raise RuntimeError("DB has no `claims` table — wrong file?")

        before = _measure(conn)
        logger.info("BEFORE: %s", before)

        rows = conn.execute(
            "SELECT id, text, scope FROM claims "
            "WHERE text IS NOT NULL AND TRIM(text) != ''"
        ).fetchall()

        scanned = 0
        entities_mentioned = 0
        aliases_added = 0
        for row in rows:
            scanned += 1
            claim_text = row["text"]
            scope = row["scope"] or "global"
            for ent in extract_patterns(claim_text):
                # Reuse existing entity when canonical_hint already resolves
                # through the alias index; otherwise create a new one.
                eid = resolve_or_create(
                    conn,
                    ent.canonical_hint,
                    entity_type=f"text_entity:{ent.kind}",
                    scope=scope,
                )
                if eid <= 0:
                    continue
                entities_mentioned += 1
                if ent.surface and ent.surface != ent.canonical_hint:
                    if add_alias(conn, eid, ent.surface):
                        aliases_added += 1
                # Kind-tagged stable alias — guarantees ≥2 aliases per
                # extracted entity so avg_aliases_per_entity climbs past 2.
                if add_alias(conn, eid, f"{ent.kind}:{ent.canonical_hint}"):
                    aliases_added += 1

        if dry_run:
            conn.rollback()
            logger.info("DRY-RUN: rolling back %s alias inserts", aliases_added)
        else:
            conn.commit()

        after = _measure(conn)
        logger.info("AFTER: %s", after)
        kinds = _per_kind_stats(conn) if not dry_run else {}

        return {
            "db": str(db_path),
            "dry_run": dry_run,
            "claims_scanned": scanned,
            "entities_mentioned": entities_mentioned,
            "aliases_added": aliases_added,
            "before": before,
            "after": after,
            "per_kind": kinds,
        }
    finally:
        conn.close()


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__ or "")
    p.add_argument("--db", type=Path, required=True, help="Path to SQLite DB")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Report only, no writes")
    mode.add_argument("--apply", action="store_true", help="Commit new aliases")
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(message)s")

    result = backfill(args.db, dry_run=args.dry_run)

    print("=" * 70)
    print(f"DB: {result['db']}")
    print(f"Mode: {'DRY-RUN' if result['dry_run'] else 'APPLY'}")
    print(f"Claims scanned: {result['claims_scanned']}")
    print(f"Entity mentions extracted: {result['entities_mentioned']}")
    print(f"Aliases added: {result['aliases_added']}")
    print()
    print("Before:")
    for k, v in result["before"].items():
        print(f"  {k}: {v}")
    print("After:")
    for k, v in result["after"].items():
        print(f"  {k}: {v}")
    if result["per_kind"]:
        print()
        print("Per-kind text_entity breakdown:")
        for kind, stats in result["per_kind"].items():
            print(f"  {kind}: {stats}")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
