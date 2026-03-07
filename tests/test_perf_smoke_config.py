from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PERF_SMOKE_PATH = REPO_ROOT / "benchmarks" / "perf_smoke.py"


def _load_perf_smoke_module():
    spec = importlib.util.spec_from_file_location("perf_smoke_test_module", PERF_SMOKE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load benchmarks/perf_smoke.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module.__name__] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _tmp_dir(prefix: str) -> Path:
    base = REPO_ROOT / ".tmp_pytest"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=str(base)))


def test_load_thresholds_config_merges_defaults() -> None:
    mod = _load_perf_smoke_module()
    case_dir = _tmp_dir("perf-smoke-config")
    cfg = case_dir / "slo_targets.json"
    cfg.write_text(
        json.dumps(
            {
                "query_p95_seconds_max": 0.3,
                "total_runtime_seconds_max": 30,
            }
        ),
        encoding="utf-8",
    )

    thresholds, source = mod.load_thresholds_config(cfg)

    assert source.endswith("slo_targets.json")
    assert thresholds.query_p95_seconds_max == 0.3
    assert thresholds.total_runtime_seconds_max == 30.0
    assert thresholds.ingest_p95_seconds_max == mod.DEFAULT_INGEST_P95_MAX
    assert thresholds.query_throughput_min_ops_per_sec == mod.DEFAULT_QUERY_THROUGHPUT_MIN


def test_load_thresholds_config_rejects_unknown_keys() -> None:
    mod = _load_perf_smoke_module()
    case_dir = _tmp_dir("perf-smoke-config-unknown")
    cfg = case_dir / "bad_targets.json"
    cfg.write_text(json.dumps({"unknown_key": 123}), encoding="utf-8")

    try:
        mod.load_thresholds_config(cfg)
    except ValueError as exc:
        assert "Unknown SLO config keys" in str(exc)
    else:
        raise AssertionError("Expected ValueError for unknown SLO key")


def test_apply_threshold_overrides_only_changes_specified_values() -> None:
    mod = _load_perf_smoke_module()
    base = mod.Thresholds(
        ingest_p95_seconds_max=0.2,
        ingest_throughput_min_ops_per_sec=55.0,
        query_p95_seconds_max=0.4,
        query_throughput_min_ops_per_sec=8.0,
        cycle_p95_seconds_max=9.0,
        total_runtime_seconds_max=70.0,
    )
    args = argparse.Namespace(
        ingest_p95_max=None,
        ingest_throughput_min=99.0,
        query_p95_max=0.22,
        query_throughput_min=None,
        cycle_p95_max=None,
        total_runtime_max=44.0,
    )

    merged = mod.apply_threshold_overrides(base, args)

    assert merged.ingest_p95_seconds_max == 0.2
    assert merged.ingest_throughput_min_ops_per_sec == 99.0
    assert merged.query_p95_seconds_max == 0.22
    assert merged.query_throughput_min_ops_per_sec == 8.0
    assert merged.cycle_p95_seconds_max == 9.0
    assert merged.total_runtime_seconds_max == 44.0
