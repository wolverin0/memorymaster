"""Sample Dreaming sources read-only, including zero-output captures, without exporting text."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memorymaster.dreaming.sampling import sample_sources  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--since", required=True, help="Inclusive ISO timestamp with timezone")
    parser.add_argument("--until", required=True, help="Exclusive ISO timestamp with timezone")
    parser.add_argument("--per-stratum", type=int, default=5)
    parser.add_argument("--out", type=Path, help="Optional content-free JSON manifest")
    args = parser.parse_args()
    if args.out and (args.out.resolve() == args.ledger.resolve()
                     or args.out.suffix.lower() != ".json"):
        parser.error("--out must be a JSON report, never the source ledger")
    report = sample_sources(args.ledger, since=args.since, until=args.until,
                            per_stratum=args.per_stratum)
    output = json.dumps(report, sort_keys=True, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output + "\n", encoding="utf-8")
    print(output)
    # Completion proves sampling only. No activation decision is made here.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
