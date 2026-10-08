"""Run a deterministic LongMemEval slice, persist its gate metrics and enforce an acceptance policy.

Exit 0 means the measurement completed AND satisfied the policy; exit 1 means it
completed but violated it (the violations are listed in the output). Before
2026-10-03 the gate always exited 0, so a degraded ranking still "passed".
Always enforced: a complete slice (questions == --limit) and no provider calls
(unless --max-provider-calls is raised). Opt-in: --require-rankings-match,
--min-mrr, --min-recall-at-5.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _signature(
    payload: dict[str, Any],
    *,
    offset: int,
    limit: int,
) -> list[tuple[Any, ...]]:
    rows = payload["retrieval"]["results"][offset : offset + limit]
    return [
        (
            row["question_id"],
            tuple(row["top_session_ids"]),
            row["reciprocal_rank"],
            row["recall_at_5"],
            row["recall_at_10"],
        )
        for row in rows
    ]


def _run_benchmark(output: Path, limit: int, offset: int) -> None:
    env = {**os.environ, "MEMORYMASTER_LLM_RERANK": "0"}
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests" / "bench_longmemeval.py"),
            "--retrieval-only",
            "--limit",
            str(limit),
            "--offset",
            str(offset),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        env=env,
        check=True,
    )


def _persist_gate_summary(
    output: Path,
    payload: dict[str, Any],
    summary: dict[str, float | int],
) -> None:
    """Keep detailed evidence while exposing stable top-level gate metrics."""
    persisted = {**payload, **summary}
    output.write_text(
        json.dumps(persisted, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _violations(result: dict[str, float | int], args: argparse.Namespace) -> list[str]:
    found: list[str] = []
    if result["questions"] != args.limit:
        found.append(f"incomplete: {result['questions']} questions, expected {args.limit}")
    if result["provider_calls"] > args.max_provider_calls:
        found.append(f"provider_calls {result['provider_calls']} > {args.max_provider_calls}")
    if args.require_rankings_match and not result["rankings_match"]:
        found.append("rankings differ from the baseline window")
    if args.min_mrr is not None and result["mrr"] < args.min_mrr:
        found.append(f"mrr {result['mrr']:.4f} < {args.min_mrr}")
    if args.min_recall_at_5 is not None and result["recall_at_5"] < args.min_recall_at_5:
        found.append(f"recall_at_5 {result['recall_at_5']:.4f} < {args.min_recall_at_5}")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--require-rankings-match", action="store_true")
    parser.add_argument("--min-mrr", type=float)
    parser.add_argument("--min-recall-at-5", type=float)
    parser.add_argument("--max-provider-calls", type=int, default=0)
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be greater than zero")
    if args.offset < 0:
        parser.error("--offset must be zero or greater")

    _run_benchmark(args.output, args.limit, args.offset)
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    current = json.loads(args.output.read_text(encoding="utf-8"))
    retrieval = current["retrieval"]
    metrics = retrieval["metrics"]
    result = {
        "elapsed_seconds": float(retrieval["elapsed_seconds"]),
        "rankings_match": int(
            _signature(current, offset=0, limit=args.limit)
            == _signature(baseline, offset=args.offset, limit=args.limit)
        ),
        "recall_at_5": float(metrics["recall_at_5"]),
        "recall_at_10": float(metrics["recall_at_10"]),
        "mrr": float(metrics["mrr"]),
        "questions": int(metrics["count"]),
        "offset": int(args.offset),
        "provider_calls": int(retrieval["llm_rerank"]["approx_calls"]),
    }
    violations = _violations(result, args)
    result["verdict"] = "fail" if violations else "pass"
    result["violations"] = violations
    _persist_gate_summary(args.output, current, result)
    print(json.dumps(result, sort_keys=True))
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
