"""Export Jev decisions as training / off-policy-evaluation JSONL (one row per decided item).

Thin wrapper over ``memorymaster.decisions.export.export_jsonl``: question versions,
answer distributions, actions, logging propensities, exposure and joined outcomes
with their ``label_source``.  ``--split-at`` adds the time-split flag (``train``
before, ``test`` after).  The ledger is opened read-only and never created.

    python scripts/jev_export.py --output decisions.jsonl --split-at 2026-09-16T00:00:00Z
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=None, help="decisions ledger (default: MEMORYMASTER_DECISIONS_DB)")
    parser.add_argument("--output", required=True, help="JSONL file to write")
    parser.add_argument("--since", type=parse_time, default=None, help="ISO-8601 lower bound on decision time")
    parser.add_argument("--until", type=parse_time, default=None, help="ISO-8601 upper bound on decision time")
    parser.add_argument("--split-at", type=parse_time, default=None,
                        help="ISO-8601 time split: earlier decisions are 'train', later ones 'test'")
    parser.add_argument("--include-state", action="store_true", help="include the redacted request state")
    return parser


def main(argv: list[str] | None = None) -> int:
    from memorymaster.decisions.export import export_jsonl
    from memorymaster.surfaces.jev_review import read_ledger, refuse_ledger_output

    parser = _parser()
    args = parser.parse_args(argv)
    ledger = read_ledger(args.db)
    try:
        output = refuse_ledger_output(ledger, args.output)
    except ValueError as exc:
        parser.error(str(exc))
    rows = export_jsonl(ledger, output, since=args.since, until=args.until,
                        split_at=args.split_at, include_state=args.include_state)
    print(f"exported {rows} rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
