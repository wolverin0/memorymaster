"""One-shot lifecycle-safe archival of session-state.watchkeeper heartbeat candidates.

Heartbeats are volatile operational pulses (kind=watchkeeper_heartbeat) that were
historically routed into the claims table before the WispBOT/WatchKeeper producer
fix (whatsapp-bot 23baf05b). They have no truth value to validate, no freshness to
decay, and no supersession semantics, so they pollute the candidate pool.

EXACT selector (and nothing else): status='candidate' AND scope='session-state.watchkeeper'.
Does NOT touch confirmed watchkeeper claims or any other scope (e.g. project:memorymaster).

Each archival goes through lifecycle.transition_claim() so a transition event is
recorded per claim (audit trail preserved). Dry-run by default.

Usage:
    python scripts/archive_watchkeeper_heartbeats.py            # dry-run (no writes)
    python scripts/archive_watchkeeper_heartbeats.py --apply    # perform archival
    python scripts/archive_watchkeeper_heartbeats.py --db other.db --apply
"""
from __future__ import annotations

import argparse
import sys

from memorymaster.core.lifecycle import transition_claim
from memorymaster.stores.storage import SQLiteStore

SCOPE = "session-state.watchkeeper"
REASON = "heartbeat session-state pulse — not a durable claim (WatchKeeper firehose cleanup)"


def _candidate_ids(store: SQLiteStore) -> list[int]:
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM claims WHERE status = ? AND scope = ? ORDER BY id",
            ("candidate", SCOPE),
        ).fetchall()
    return [int(r[0]) for r in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="memorymaster.db", help="SQLite DB path")
    parser.add_argument("--apply", action="store_true", help="Perform archival (default: dry-run)")
    args = parser.parse_args(argv)

    store = SQLiteStore(args.db)
    ids = _candidate_ids(store)

    print(f"DB: {args.db}")
    print(f"Selector: status='candidate' AND scope='{SCOPE}'")
    print(f"Matched candidate heartbeats: {len(ids)}")

    if not ids:
        print("Nothing to archive.")
        return 0

    if not args.apply:
        print("[DRY RUN] No claims archived. Re-run with --apply to perform the archival.")
        print(f"[DRY RUN] Would archive {len(ids)} claims (candidate -> archived), one transition event each.")
        return 0

    archived = 0
    errors = 0
    for claim_id in ids:
        try:
            transition_claim(store, claim_id, "archived", REASON, event_type="transition")
            archived += 1
        except ValueError as exc:
            errors += 1
            print(f"  skip claim {claim_id}: {exc}", file=sys.stderr)
        if archived % 2000 == 0 and archived:
            print(f"  ... archived {archived}/{len(ids)}")

    print(f"Archived: {archived}  Errors/skipped: {errors}")
    remaining = len(_candidate_ids(store))
    print(f"Remaining candidate heartbeats after run: {remaining}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
