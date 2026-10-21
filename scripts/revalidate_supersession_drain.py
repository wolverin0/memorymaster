"""Census legacy supersession claims staled by the conflict drain.

The default is read-only. Revalidation requires --apply and one or more
--human-id arguments; it uses the automatic validator's existing policy gates.
No schema initialization, steward cycle, or explicit-actor override is used.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys


_DRAIN_REASON = "conflicto sin rival confirmado vigente"
_CENSUS_SQL = """
    SELECT c.id, c.human_id FROM claims AS c
    WHERE c.status = 'stale' AND c.supersedes_claim_id IS NOT NULL
      AND EXISTS (
          SELECT 1 FROM events AS e WHERE e.claim_id = c.id
            AND e.event_type = 'transition' AND e.to_status = 'stale'
            AND instr(e.details, ?) > 0
      )
    ORDER BY c.human_id, c.id
"""


def census(db_path: Path) -> list[tuple[int, str | None]]:
    """Read affected identities without opening a writable store connection."""
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.execute("PRAGMA query_only = ON")
        return [(row[0], row[1]) for row in conn.execute(_CENSUS_SQL, (_DRAIN_REASON,))]


def apply_selected(db_path: Path, human_ids: list[str]) -> dict[str, int]:
    """Validate the entire selection before allowing any validator writes."""
    from memorymaster.govern.jobs import validator
    from memorymaster.stores.storage import SQLiteStore

    requested = list(dict.fromkeys(human_ids))
    matches = census(db_path)
    selected_ids: list[int] = []
    for human_id in requested:
        ids = [claim_id for claim_id, matched_id in matches if matched_id == human_id]
        if len(ids) != 1:
            raise ValueError("Every selected human ID must uniquely match the census.")
        selected_ids.append(ids[0])

    # Hydrate through read-only connections, then recheck census membership.
    # A rejected batch must never invoke a writable connection or init_db.
    reader = SQLiteStore(db_path, read_only=True)
    selected = [reader.get_claim(claim_id, include_citations=False) for claim_id in selected_ids]
    current_members = set(census(db_path))
    for human_id, claim in zip(requested, selected):
        if (
            claim is None
            or claim.status != "stale"
            or claim.supersedes_claim_id is None
            or claim.human_id != human_id
            or (claim.id, human_id) not in current_members
        ):
            raise ValueError("Selection changed; run the census again before applying.")
        if claim.pinned:
            raise ValueError("Pinned claims require a separate operator decision.")

    return validator.run(
        SQLiteStore(db_path),
        limit=0,
        revalidation_claims=selected,
        policy_mode="revalidation",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("MEMORYMASTER_DB"), help="Existing SQLite DB")
    parser.add_argument("--apply", action="store_true", help="Revalidate explicitly selected claims")
    parser.add_argument("--human-id", action="append", default=[], help="Selected census ID; repeatable")
    args = parser.parse_args(argv)
    if not args.db:
        parser.error("Provide --db or set MEMORYMASTER_DB.")
    if args.apply and not args.human_id:
        parser.error("--apply requires at least one --human-id.")
    if args.human_id and not args.apply:
        parser.error("--human-id requires --apply.")

    try:
        db_path = Path(args.db)
        if args.apply:
            result = apply_selected(db_path, args.human_id)
            report = {
                "count": len(set(args.human_id)),
                "human_ids": list(dict.fromkeys(args.human_id)),
                "result": result,
            }
        else:
            matches = census(db_path)
            report = {"count": len(matches), "human_ids": [human_id for _, human_id in matches]}
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError):
        # Database/OS diagnostics may contain private paths or claim text.
        print(json.dumps({"error": "Unable to process the existing SQLite database."}), file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
