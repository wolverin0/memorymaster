"""Backfill Layer-1 (and optional Layer-2 LLM) entity extraction.

Scans every row in ``claims`` and registers extracted entities (files,
env-vars, services, ports, commits, tools) as aliases on virtual
``(text_entity:<kind>:<canonical_hint>)`` entities. Idempotent: re-running
produces 0 new rows because the ``entity_aliases`` UNIQUE constraint on
``(entity_id, variant_key)`` dedupes variant inserts.

Layer 2 (``--layer2``) adds an LLM pass for kinds the regex cannot catch:
``person_name``, ``spanish_surname``, ``time_expression``, ``model_name``,
``library_name``, ``concept``. Requires ``MEMORYMASTER_ENTITY_LLM=1`` and
a configured provider (``GEMINI_API_KEY`` etc.). Costs real money on real
providers — start with ``--dry-run --limit 100``.

Usage
-----

    # Dry-run, Layer-1 only
    python scripts/backfill_entity_extraction.py --db copy.db --dry-run

    # Dry-run, Layer-1 + Layer-2, 100 claims
    MEMORYMASTER_ENTITY_LLM=1 python scripts/backfill_entity_extraction.py \\
        --db copy.db --dry-run --layer2 --limit 100

    # Apply, Layer-1 only (safe)
    python scripts/backfill_entity_extraction.py --db copy.db --apply

WARNING: never run --apply against the live memorymaster.db. Always work
off a copy. The live DB is ~8 GB and concurrent writers can corrupt it.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

# Keep the import relative to repo root so running from /scripts works.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from memorymaster.entity_extractor import (  # noqa: E402
    extract_llm,
    extract_patterns,
    merge_entities,
)
from memorymaster.entity_registry import (  # noqa: E402
    add_alias,
    ensure_entity_schema,
    resolve_or_create,
)
from memorymaster.security import redact_text  # noqa: E402

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


def _prepare_text_for_llm(claim_text: str) -> tuple[str | None, list[str]]:
    """Run sensitivity filter before sending to LLM.

    Returns (redacted_text, findings). If redaction found secrets, we send
    the REDACTED text (not the original) — the claim stays in the DB but
    the LLM never sees raw credentials. If redaction stripped so much that
    the remainder is empty/unhelpful, returns (None, findings) to signal
    the caller to skip Layer-2 for this claim.
    """
    redacted, findings = redact_text(claim_text)
    if redacted is None or not redacted.strip():
        return None, findings
    # If redaction reduced the text to mostly markers, skip.
    if len(redacted.strip()) < 16:
        return None, findings
    return redacted, findings


def backfill(
    db_path: Path,
    *,
    dry_run: bool,
    layer2: bool = False,
    limit: int | None = None,
    progress_every: int = 50,
) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")

    if layer2 and os.environ.get("MEMORYMASTER_ENTITY_LLM", "").strip().lower() in {
        "",
        "0",
        "false",
        "no",
        "off",
    }:
        logger.warning(
            "--layer2 passed but MEMORYMASTER_ENTITY_LLM is unset; "
            "extract_llm() will be a no-op. Set the env var to enable."
        )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        ensure_entity_schema(conn)
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='claims'"
        ).fetchone()
        if not exists:
            raise RuntimeError("DB has no `claims` table — wrong file?")

        before = _measure(conn)
        logger.info("BEFORE: %s", before)

        sql = (
            "SELECT id, text, scope FROM claims "
            "WHERE text IS NOT NULL AND TRIM(text) != ''"
        )
        if limit is not None and limit > 0:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql).fetchall()
        total = len(rows)

        scanned = 0
        entities_mentioned = 0
        aliases_added = 0
        llm_calls = 0
        llm_skipped_sensitive = 0
        llm_entities_added = 0
        t_start = time.monotonic()

        for row in rows:
            scanned += 1
            claim_text = row["text"]
            scope = row["scope"] or "global"

            l1 = extract_patterns(claim_text)
            l2: list = []
            if layer2:
                safe_text, findings = _prepare_text_for_llm(claim_text)
                if safe_text is None:
                    llm_skipped_sensitive += 1
                else:
                    l2 = extract_llm(safe_text)
                    if l2:
                        llm_calls += 1
                    else:
                        # Even an empty response counts as a call if LLM is on —
                        # we only know it ran because extract_llm gated on env.
                        # Leave call-count conservative: only count successful.
                        pass
                    if findings:
                        logger.debug(
                            "claim %s: redacted findings=%s", row["id"], findings
                        )
                llm_entities_added += len(l2)

            for ent in merge_entities(l1, l2):
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
                if add_alias(conn, eid, f"{ent.kind}:{ent.canonical_hint}"):
                    aliases_added += 1

            if progress_every and scanned % progress_every == 0:
                snapshot = _measure(conn)
                logger.info(
                    "Processed %d/%d, LLM calls: %d, avg_aliases: %s",
                    scanned,
                    total,
                    llm_calls,
                    snapshot["avg_aliases_per_entity"],
                )

        if dry_run:
            conn.rollback()
            logger.info("DRY-RUN: rolling back %s alias inserts", aliases_added)
        else:
            conn.commit()

        after = _measure(conn)
        logger.info("AFTER: %s", after)
        kinds = _per_kind_stats(conn) if not dry_run else {}
        duration = round(time.monotonic() - t_start, 2)

        return {
            "db": str(db_path),
            "dry_run": dry_run,
            "layer2": layer2,
            "limit": limit,
            "claims_scanned": scanned,
            "entities_mentioned": entities_mentioned,
            "aliases_added": aliases_added,
            "llm_calls": llm_calls,
            "llm_entities_added": llm_entities_added,
            "llm_skipped_sensitive": llm_skipped_sensitive,
            "duration_s": duration,
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
        "--layer2",
        action="store_true",
        help=(
            "Also run Layer-2 LLM extractor. Requires MEMORYMASTER_ENTITY_LLM=1 "
            "and a configured LLM provider."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only scan the first N claims (useful for --layer2 dry-runs).",
    )
    p.add_argument(
        "--progress-every",
        type=int,
        default=50,
        help="Log progress every N claims (default 50, 0 to disable).",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(message)s")

    result = backfill(
        args.db,
        dry_run=args.dry_run,
        layer2=args.layer2,
        limit=args.limit,
        progress_every=args.progress_every,
    )

    print("=" * 70)
    print(f"DB: {result['db']}")
    print(f"Mode: {'DRY-RUN' if result['dry_run'] else 'APPLY'}")
    print(f"Layer-2 LLM: {'ON' if result['layer2'] else 'off'}")
    if result["limit"]:
        print(f"Limit: {result['limit']}")
    print(f"Duration: {result['duration_s']}s")
    print(f"Claims scanned: {result['claims_scanned']}")
    print(f"Entity mentions extracted: {result['entities_mentioned']}")
    print(f"Aliases added: {result['aliases_added']}")
    if result["layer2"]:
        print(f"LLM calls (productive): {result['llm_calls']}")
        print(f"LLM entities added: {result['llm_entities_added']}")
        print(f"LLM skipped (sensitive): {result['llm_skipped_sensitive']}")
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
