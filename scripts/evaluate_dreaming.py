"""Evaluate labeled native-Dreaming decisions without touching the claims DB."""

from __future__ import annotations

import argparse
import json

from memorymaster.dreaming.evaluation import evaluate_records, load_jsonl


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labels", help="JSONL file containing human-labeled Dreaming decisions")
    args = parser.parse_args()
    report = evaluate_records(load_jsonl(args.labels))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["activation_ready"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
