#!/usr/bin/env python
"""Read-only sweep (T-2276): confirmed claims whose text names an earlier claim id as corrected, with no pointers.

Lists ``old_id new_id scope`` for every pair where the newer claim's text names the older one with a correction cue
(same detector the live ingest safety net uses, ``govern.correction_text``), both are confirmed, the older one is
still live and the pair has no ``supersedes_claim_id`` / ``replaced_by_claim_id`` closing it. Ids and scopes only,
never claim text. Applies nothing: the database is opened ``mode=ro`` and the operator decides each pair (for
example by approving a ``prose-correction`` proposal or re-ingesting the correction with ``supersedes``).

    python scripts/find_prose_corrections.py --db memorymaster.db [--counts]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memorymaster.govern.correction_text import find_corrected_references  # noqa: E402

_PREFILTER = "(text LIKE '%mm-%' OR text LIKE '%claim%')"


def _target(con: sqlite3.Connection, row: sqlite3.Row, kind: str, value: str | int) -> sqlite3.Row | None:
    """The claim ``value`` names inside the newer claim's tenant and scope (same boundary as live ingest)."""
    if kind == "human_id":
        found = con.execute(
            "SELECT * FROM claims WHERE human_id = ? AND tenant_id IS ? AND scope = ? LIMIT 2",
            (value, row["tenant_id"], row["scope"]),
        ).fetchall()
        target = found[0] if len(found) == 1 else None
    else:
        target = con.execute("SELECT * FROM claims WHERE id = ?", (value,)).fetchone()
    if target is None or target["tenant_id"] != row["tenant_id"] or target["scope"] != row["scope"]:
        return None
    if target["visibility"] != "public" and (target["visibility"], target["source_agent"]) != (
        row["visibility"], row["source_agent"],
    ):
        return None
    return target


def sweep(db: str) -> dict:
    con = sqlite3.connect(f"file:{Path(db).resolve().as_posix()}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        scanned = 0
        pairs: list[tuple[int, int, str]] = []
        for row in con.execute(f"SELECT * FROM claims WHERE status = 'confirmed' AND {_PREFILTER} ORDER BY id"):
            scanned += 1
            for ref in find_corrected_references(row["text"] or ""):
                old = _target(con, row, ref.kind, ref.value)
                if (
                    old is None or old["id"] >= row["id"] or old["status"] != "confirmed"
                    or old["replaced_by_claim_id"] is not None or row["supersedes_claim_id"] == old["id"]
                ):
                    continue
                pairs.append((old["id"], row["id"], row["scope"]))
        return {"scanned": scanned, "pairs": sorted(set(pairs))}
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default="memorymaster.db")
    parser.add_argument("--counts", action="store_true", help="print only the counts")
    args = parser.parse_args(argv)
    if not Path(args.db).is_file():
        print(f"error: database not found: {args.db}", file=sys.stderr)
        return 2
    result = sweep(args.db)
    if not args.counts:
        for old_id, new_id, scope in result["pairs"]:
            print(f"{old_id}\t{new_id}\t{scope}")
    print(f"scanned_candidates={result['scanned']} uncorrected_pairs={len(result['pairs'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
