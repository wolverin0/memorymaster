"""v3.13 dedupe precision audit.

Reads the events table for `dedupe-archived:` transitions over a lookback
window, summarises daily counts, and prints N random candidate/canonical
pairs side-by-side so the operator can spot-check precision.

Usage:
    python scripts/audit_dedupe_precision.py
    python scripts/audit_dedupe_precision.py --days 7 --samples 10
    python scripts/audit_dedupe_precision.py --db memorymaster.db --days 14
"""

from __future__ import annotations

import argparse
import sqlite3
import sys


def _summary(conn: sqlite3.Connection, days: int) -> tuple[int, list[tuple[str, int]]]:
    cutoff = f"datetime('now', '-{days} days')"
    total = conn.execute(
        f"""
        SELECT COUNT(*) FROM events
        WHERE event_type='transition'
          AND details LIKE 'dedupe-archived:%'
          AND created_at > {cutoff}
        """
    ).fetchone()[0]
    by_day = conn.execute(
        f"""
        SELECT date(created_at) AS day, COUNT(*)
        FROM events
        WHERE event_type='transition'
          AND details LIKE 'dedupe-archived:%'
          AND created_at > {cutoff}
        GROUP BY day ORDER BY day DESC
        """
    ).fetchall()
    return total, by_day


def _sample(conn: sqlite3.Connection, days: int, n: int) -> list[sqlite3.Row]:
    cutoff = f"datetime('now', '-{days} days')"
    return conn.execute(
        f"""
        SELECT e.created_at, e.details,
               c.id AS cand_id, c.text AS cand_text,
               k.id AS canon_id, k.text AS canon_text, k.status AS canon_status
        FROM events e
        JOIN claims c ON c.id = e.claim_id
        LEFT JOIN claims k ON k.id = c.replaced_by_claim_id
        WHERE e.event_type='transition'
          AND e.details LIKE 'dedupe-archived:%'
          AND e.created_at > {cutoff}
        ORDER BY RANDOM() LIMIT ?
        """,
        (n,),
    ).fetchall()


def main() -> int:
    p = argparse.ArgumentParser(description="v3.13 dedupe precision audit")
    p.add_argument("--db", default="memorymaster.db")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--samples", type=int, default=10)
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    total, by_day = _summary(conn, args.days)
    print(f"# v3.13 Dedupe Precision Audit (last {args.days} days)\n")

    if total == 0:
        print("**No dedupe-archived events found in the lookback window.**")
        print()
        print("Possible causes:")
        print("- Cron not firing — check Task Scheduler / cron logs")
        print("- Dedupe disabled — check MEMORYMASTER_DEDUPE_ENABLED env in the steward hook")
        print("- Still in shadow mode — check MEMORYMASTER_DEDUPE_SHADOW (should be '0' for active)")
        print("- Genuinely no near-duplicates in this period (rare; v3.13.2 baseline was ~5 per cycle)")
        return 1

    rate = total / args.days
    print(f"**Total archives**: {total}")
    print(f"**Daily rate**: {rate:.1f} per day (baseline ~20/day at 5 per 6h cycle)")
    print()
    print("## Daily breakdown")
    print()
    print("| Day | Archives |")
    print("|-----|----------|")
    for day, count in by_day:
        print(f"| {day} | {count} |")
    print()

    rows = _sample(conn, args.days, args.samples)
    print(f"## {len(rows)} random pair samples")
    print()
    for i, row in enumerate(rows, 1):
        cand = (row["cand_text"] or "").replace("\n", " ").strip()[:300]
        canon = (row["canon_text"] or "").replace("\n", " ").strip()[:300] if row["canon_text"] else "(canonical not found — broken link?)"
        canon_status = row["canon_status"] or "?"
        print(f"### Pair {i} — {row['created_at']} — canon_status={canon_status}")
        print(f"  **Cand {row['cand_id']}**: {cand}")
        print(f"  **Canon {row['canon_id']}**: {canon}")
        print()

    print("## Verdict")
    print()
    print("Read each pair above. For each one, ask: does the candidate carry information NOT in the canonical?")
    print()
    print("- If 0-1 false positives out of 10: precision is healthy, leave threshold at 0.85")
    print("- If 2+ false positives out of 10: drop threshold by 0.05 — edit ~/.claude/hooks/memorymaster-steward-cycle.py to set MEMORYMASTER_DEDUPE_JACCARD_HIGH=0.90")
    print("- If most pairs are unrelated: something is very wrong; revert by setting MEMORYMASTER_DEDUPE_SHADOW=1 and investigate")

    return 0


if __name__ == "__main__":
    sys.exit(main())
