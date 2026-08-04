"""Emit compact, repeat-stabilized MemoryMaster performance metrics for autoresearch."""

from __future__ import annotations

import argparse
import contextlib
import gc
import importlib.util
import io
import json
import os
import statistics
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PERF_SMOKE_PATH = ROOT / "benchmarks" / "perf_smoke.py"


def _load_perf_smoke() -> Any:
    spec = importlib.util.spec_from_file_location("memorymaster_autoresearch_perf_smoke", PERF_SMOKE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {PERF_SMOKE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _deterministic_environment() -> Any:
    names = ("QDRANT_URL", "MEMORYMASTER_LLM_RERANK", "MEMORYMASTER_RECALL_RERANK")
    original = {name: os.environ.get(name) for name in names}
    os.environ.pop("QDRANT_URL", None)
    os.environ["MEMORYMASTER_LLM_RERANK"] = "0"
    os.environ["MEMORYMASTER_RECALL_RERANK"] = "0"
    try:
        yield
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _median(samples: list[dict[str, Any]], *path: str) -> float:
    values: list[float] = []
    for sample in samples:
        value: Any = sample
        for key in path:
            value = value[key]
        values.append(float(value))
    return statistics.median(values)


def _aggregate(samples: list[dict[str, Any]], expected_claims: int) -> dict[str, float | int]:
    query_p95_values = [float(sample["timing"]["query"]["p95_seconds"]) for sample in samples]
    return {
        "query_p95_seconds": statistics.median(query_p95_values),
        "query_p95_spread_seconds": max(query_p95_values) - min(query_p95_values),
        "query_throughput_ops_per_sec": _median(samples, "timing", "query", "throughput_ops_per_sec"),
        "ingest_p95_seconds": _median(samples, "timing", "ingest", "p95_seconds"),
        "cycle_p95_seconds": _median(samples, "timing", "cycle", "p95_seconds"),
        "total_runtime_seconds": _median(samples, "timing", "total_runtime_seconds"),
        "query_misses": max(int(sample["timing"]["query"]["misses"]) for sample in samples),
        "confirmed_claims": min(
            int(sample["quality"]["confirmed_claims_after_cycles"]) for sample in samples
        ),
        "expected_claims": expected_claims,
        "repeats": len(samples),
        "provider_calls": 0,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims", type=int, default=80)
    parser.add_argument("--queries", type=int, default=30)
    parser.add_argument("--cycles", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=3)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if min(args.claims, args.queries, args.repeats) <= 0 or args.cycles < 0:
        raise SystemExit("claims, queries, and repeats must be positive; cycles cannot be negative")
    perf_smoke = _load_perf_smoke()
    samples: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="mm-autoresearch-perf-") as tmp, _deterministic_environment():
        old_cwd = Path.cwd()
        try:
            os.chdir(tmp)
            for _ in range(args.repeats):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    samples.append(
                        perf_smoke.run_perf_smoke(
                            claims=args.claims,
                            queries=args.queries,
                            cycles=args.cycles,
                            workspace_root=ROOT,
                        )
                    )
                gc.collect()
        finally:
            os.chdir(old_cwd)
    print(json.dumps(_aggregate(samples, args.claims), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
