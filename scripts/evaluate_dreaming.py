"""Evaluate labeled native-Dreaming decisions without touching the claims DB."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from memorymaster.dreaming.evaluation import evaluate_records, load_jsonl  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labels", help="JSONL file containing human-labeled Dreaming decisions")
    parser.add_argument("--cohort", type=Path, help="Frozen cohort directory; verifies fingerprint and full source coverage")
    parser.add_argument("--human-labels", type=Path, help="Separate explicit human judgments over the AI preparation")
    args = parser.parse_args()
    if args.human_labels and not args.cohort:
        parser.error("--human-labels requires --cohort")
    if args.cohort:
        from memorymaster.dreaming.cohort_evaluation import evaluate_cohort

        report = evaluate_cohort(
            json.loads((args.cohort / "manifest.json").read_text(encoding="utf-8")),
            json.loads((args.cohort / "sources.json").read_text(encoding="utf-8")),
            load_jsonl(args.labels), load_jsonl(args.human_labels) if args.human_labels else [],
        )
    else:
        report = evaluate_records(load_jsonl(args.labels))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["activation_ready"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
