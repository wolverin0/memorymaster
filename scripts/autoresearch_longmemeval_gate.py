"""Run a deterministic LongMemEval slice and emit one metrics JSON line."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _signature(payload: dict[str, Any], limit: int) -> list[tuple[Any, ...]]:
    rows = payload["retrieval"]["results"][:limit]
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


def _run_benchmark(output: Path, limit: int) -> None:
    env = {**os.environ, "MEMORYMASTER_LLM_RERANK": "0"}
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests" / "bench_longmemeval.py"),
            "--retrieval-only",
            "--limit",
            str(limit),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        env=env,
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()

    _run_benchmark(args.output, args.limit)
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    current = json.loads(args.output.read_text(encoding="utf-8"))
    retrieval = current["retrieval"]
    metrics = retrieval["metrics"]
    result = {
        "elapsed_seconds": float(retrieval["elapsed_seconds"]),
        "rankings_match": int(_signature(current, args.limit) == _signature(baseline, args.limit)),
        "recall_at_5": float(metrics["recall_at_5"]),
        "recall_at_10": float(metrics["recall_at_10"]),
        "mrr": float(metrics["mrr"]),
        "questions": int(metrics["count"]),
        "provider_calls": int(retrieval["llm_rerank"]["approx_calls"]),
    }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
