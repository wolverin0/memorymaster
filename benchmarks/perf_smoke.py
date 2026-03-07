from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memorymaster.models import CitationInput
from memorymaster.service import MemoryService

DEFAULT_INGEST_P95_MAX = 0.060
DEFAULT_INGEST_THROUGHPUT_MIN = 80.0
DEFAULT_QUERY_P95_MAX = 0.250
DEFAULT_QUERY_THROUGHPUT_MIN = 12.0
DEFAULT_CYCLE_P95_MAX = 3.5
DEFAULT_TOTAL_RUNTIME_MAX = 20.0
DEFAULT_SLO_CONFIG_PATH = "benchmarks/slo_targets.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def normalize_fs_path(path: Path | str) -> str:
    value = str(path)
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil((p / 100.0) * len(ordered)))
    return ordered[rank - 1]


@dataclass(slots=True)
class Thresholds:
    ingest_p95_seconds_max: float = DEFAULT_INGEST_P95_MAX
    ingest_throughput_min_ops_per_sec: float = DEFAULT_INGEST_THROUGHPUT_MIN
    query_p95_seconds_max: float = DEFAULT_QUERY_P95_MAX
    query_throughput_min_ops_per_sec: float = DEFAULT_QUERY_THROUGHPUT_MIN
    cycle_p95_seconds_max: float = DEFAULT_CYCLE_P95_MAX
    total_runtime_seconds_max: float = DEFAULT_TOTAL_RUNTIME_MAX


def load_thresholds_config(config_path: Path | None, *, profile: str = "quick") -> tuple[Thresholds, str]:
    if config_path is None:
        return Thresholds(), f"builtin_defaults:{profile}"
    if not config_path.exists():
        raise FileNotFoundError(f"SLO config file not found: {config_path}")

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"SLO config must be a JSON object: {config_path}")

    resolved_payload = payload
    profiles = payload.get("profiles")
    if isinstance(profiles, dict):
        profile_name = profile.strip().lower() or "quick"
        candidate = profiles.get(profile_name)
        if not isinstance(candidate, dict):
            available = ", ".join(sorted(_to_str(key) for key in profiles.keys()))
            raise ValueError(
                f"SLO profile '{profile_name}' not found in {config_path}. Available profiles: {available}"
            )
        resolved_payload = candidate

    defaults = Thresholds()
    raw_values: dict[str, Any] = asdict(defaults)
    allowed = {f.name for f in fields(Thresholds)}
    unknown_keys = sorted(key for key in resolved_payload if key not in allowed)
    if unknown_keys:
        raise ValueError(
            "Unknown SLO config keys: "
            + ", ".join(unknown_keys)
            + f" (allowed: {', '.join(sorted(allowed))})"
        )

    for key, value in resolved_payload.items():
        raw_values[key] = float(value)
    source = str(config_path)
    if isinstance(profiles, dict):
        source = f"{config_path}#profile={profile.strip().lower() or 'quick'}"
    return Thresholds(**raw_values), source


def apply_threshold_overrides(base: Thresholds, args: argparse.Namespace) -> Thresholds:
    data = asdict(base)
    arg_map = {
        "ingest_p95_seconds_max": "ingest_p95_max",
        "ingest_throughput_min_ops_per_sec": "ingest_throughput_min",
        "query_p95_seconds_max": "query_p95_max",
        "query_throughput_min_ops_per_sec": "query_throughput_min",
        "cycle_p95_seconds_max": "cycle_p95_max",
        "total_runtime_seconds_max": "total_runtime_max",
    }
    for key, arg_name in arg_map.items():
        value = getattr(args, arg_name)
        if value is not None:
            data[key] = float(value)
    return Thresholds(**data)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a short ingest/query/cycle performance smoke check.")
    parser.add_argument("--claims", type=int, default=80, help="Number of synthetic claims to ingest.")
    parser.add_argument("--queries", type=int, default=30, help="Number of synthetic queries to execute.")
    parser.add_argument("--cycles", type=int, default=2, help="Number of maintenance cycles to execute.")
    parser.add_argument("--workspace", default=".", help="Workspace root for deterministic validators.")
    parser.add_argument("--out-json", default="artifacts/perf/perf_smoke.json", help="Output JSON report path.")
    parser.add_argument(
        "--slo-config",
        default=DEFAULT_SLO_CONFIG_PATH,
        help="JSON file with Thresholds keys (default: benchmarks/slo_targets.json).",
    )
    parser.add_argument(
        "--profile",
        default="quick",
        help="SLO profile name when config uses {\"profiles\": {...}} shape.",
    )
    parser.add_argument("--ingest-p95-max", type=float, default=None)
    parser.add_argument("--ingest-throughput-min", type=float, default=None)
    parser.add_argument("--query-p95-max", type=float, default=None)
    parser.add_argument("--query-throughput-min", type=float, default=None)
    parser.add_argument("--cycle-p95-max", type=float, default=None)
    parser.add_argument("--total-runtime-max", type=float, default=None)
    return parser.parse_args()


def run_perf_smoke(*, claims: int, queries: int, cycles: int, workspace_root: Path) -> dict[str, object]:
    overall_start = time.monotonic()
    ingest_times: list[float] = []
    query_times: list[float] = []
    cycle_times: list[float] = []
    query_misses = 0

    tmp_root = Path(normalize_fs_path(Path(".tmp_cases")))
    tmp_root.mkdir(parents=True, exist_ok=True)
    db_path = tmp_root / f"perf-smoke-{int(time.time() * 1000)}.db"
    db_path_str = normalize_fs_path(db_path)

    service = MemoryService(
        db_path_str,
        workspace_root=Path(normalize_fs_path(workspace_root)),
    )
    try:
        service.init_db()

        ingest_started = time.monotonic()
        for idx in range(claims):
            started = time.perf_counter()
            service.ingest(
                text=f"Synthetic perf claim {idx} has value value_{idx}",
                citations=[
                    CitationInput(
                        source="perf://smoke",
                        locator=f"claim-{idx}",
                        excerpt="synthetic perf fixture",
                    )
                ],
                idempotency_key=f"perf-smoke-{idx}",
                claim_type="perf_smoke",
                subject=f"entity_{idx}",
                predicate="setting",
                object_value=f"value_{idx}",
                scope="perf",
                volatility="low",
                confidence=0.72,
            )
            ingest_times.append(time.perf_counter() - started)
        ingest_total = time.monotonic() - ingest_started

        cycle_results: list[dict[str, object]] = []
        for _ in range(cycles):
            started = time.perf_counter()
            cycle_result = service.run_cycle(
                run_compactor=False,
                min_citations=1,
                min_score=0.58,
                policy_mode="legacy",
                policy_limit=200,
            )
            cycle_times.append(time.perf_counter() - started)
            cycle_results.append(cycle_result)

        query_started = time.monotonic()
        for idx in range(queries):
            target = idx % max(claims, 1)
            started = time.perf_counter()
            rows = service.query(
                query_text=f"value_{target}",
                limit=5,
                include_stale=True,
                include_conflicted=True,
                retrieval_mode="hybrid",
                allow_sensitive=False,
            )
            query_times.append(time.perf_counter() - started)
            if not rows:
                query_misses += 1
        query_total = time.monotonic() - query_started

        confirmed_claims = len(service.list_claims(status="confirmed", limit=max(claims * 2, 50)))
    finally:
        try:
            Path(db_path_str).unlink(missing_ok=True)
        except OSError:
            pass

    total_runtime = time.monotonic() - overall_start
    ingest_throughput = (claims / ingest_total) if ingest_total > 0 else 0.0
    query_throughput = (queries / query_total) if query_total > 0 else 0.0

    return {
        "timing": {
            "total_runtime_seconds": total_runtime,
            "ingest": {
                "ops": claims,
                "total_seconds": ingest_total,
                "p95_seconds": percentile(ingest_times, 95),
                "throughput_ops_per_sec": ingest_throughput,
            },
            "query": {
                "ops": queries,
                "total_seconds": query_total,
                "p95_seconds": percentile(query_times, 95),
                "throughput_ops_per_sec": query_throughput,
                "misses": query_misses,
            },
            "cycle": {
                "runs": cycles,
                "total_seconds": sum(cycle_times),
                "p95_seconds": percentile(cycle_times, 95),
                "max_seconds": max(cycle_times) if cycle_times else 0.0,
            },
        },
        "quality": {
            "confirmed_claims_after_cycles": confirmed_claims,
            "latest_cycle": cycle_results[-1] if cycle_results else {},
        },
    }


def check_thresholds(report: dict[str, object], thresholds: Thresholds, expected_claims: int) -> list[str]:
    timing = report["timing"]  # type: ignore[index]
    ingest = timing["ingest"]  # type: ignore[index]
    query = timing["query"]  # type: ignore[index]
    cycle = timing["cycle"]  # type: ignore[index]
    quality = report["quality"]  # type: ignore[index]

    failures: list[str] = []
    if float(ingest["p95_seconds"]) > thresholds.ingest_p95_seconds_max:
        failures.append(
            f"ingest_p95_seconds>{thresholds.ingest_p95_seconds_max:.3f} (actual={float(ingest['p95_seconds']):.4f})"
        )
    if float(ingest["throughput_ops_per_sec"]) < thresholds.ingest_throughput_min_ops_per_sec:
        failures.append(
            "ingest_throughput_ops_per_sec"
            f"<{thresholds.ingest_throughput_min_ops_per_sec:.1f} "
            f"(actual={float(ingest['throughput_ops_per_sec']):.2f})"
        )
    if float(query["p95_seconds"]) > thresholds.query_p95_seconds_max:
        failures.append(
            f"query_p95_seconds>{thresholds.query_p95_seconds_max:.3f} (actual={float(query['p95_seconds']):.4f})"
        )
    if float(query["throughput_ops_per_sec"]) < thresholds.query_throughput_min_ops_per_sec:
        failures.append(
            "query_throughput_ops_per_sec"
            f"<{thresholds.query_throughput_min_ops_per_sec:.1f} "
            f"(actual={float(query['throughput_ops_per_sec']):.2f})"
        )
    if int(query["misses"]) > 0:
        failures.append(f"query_misses>0 (actual={int(query['misses'])})")
    if float(cycle["p95_seconds"]) > thresholds.cycle_p95_seconds_max:
        failures.append(
            f"cycle_p95_seconds>{thresholds.cycle_p95_seconds_max:.3f} (actual={float(cycle['p95_seconds']):.4f})"
        )
    if float(timing["total_runtime_seconds"]) > thresholds.total_runtime_seconds_max:
        failures.append(
            "total_runtime_seconds"
            f">{thresholds.total_runtime_seconds_max:.1f} "
            f"(actual={float(timing['total_runtime_seconds']):.3f})"
        )
    if int(quality["confirmed_claims_after_cycles"]) < expected_claims:
        failures.append(
            "confirmed_claims_after_cycles"
            f"<{expected_claims} (actual={int(quality['confirmed_claims_after_cycles'])})"
        )
    return failures


def main() -> int:
    args = parse_args()
    slo_path = Path(args.slo_config) if str(args.slo_config).strip() else None
    thresholds, threshold_source = load_thresholds_config(slo_path, profile=str(args.profile))
    thresholds = apply_threshold_overrides(thresholds, args)
    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)

    metrics = run_perf_smoke(
        claims=int(args.claims),
        queries=int(args.queries),
        cycles=int(args.cycles),
        workspace_root=Path(args.workspace),
    )
    failures = check_thresholds(metrics, thresholds, expected_claims=int(args.claims))

    report = {
        "timestamp": utc_now(),
        "passed": len(failures) == 0,
        "threshold_source": threshold_source,
        "thresholds": asdict(thresholds),
        "metrics": metrics,
        "threshold_failures": failures,
    }
    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report, indent=2))
    if failures:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
