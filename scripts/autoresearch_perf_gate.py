"""Emit compact, repeat-stabilized MemoryMaster performance metrics for autoresearch."""

from __future__ import annotations

import argparse
import contextlib
import gc
import importlib.util
import io
import ipaddress
import json
import os
import socket
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


# The operator's shell carries the live configuration (Jev live, dreaming providers,
# keys) and the real home. Inherited, a "benchmark" could reach providers and write the
# real decisions ledger (found 2026-10-04); same list as noxfile.LIVE_CONFIG_*.
LIVE_CONFIG_PREFIXES = ("MEMORYMASTER_",)
LIVE_CONFIG_NAMES = ("TYPESAFE_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "OPENAI_API_BASE",
                     "ANTHROPIC_API_KEY", "QDRANT_URL")
# Every writable location the package resolves from the environment, sent into the tmp dir.
SCRATCH_DIRS = ("MEMORYMASTER_SPOOL_DIR", "MEMORYMASTER_SNAPSHOT_DIR", "MEMORYMASTER_PROFILE_OUTPUT_DIR",
                "MEMORYMASTER_QDRANT_OUTBOX_DIR", "MEMORYMASTER_QUARANTINE_DIR", "MEMORYMASTER_RECALL_STATE_DIR",
                "MEMORYMASTER_DAYDREAM_INGEST_DIR", "MEMORYMASTER_VAULT_DIR", "MEMORYMASTER_WIKI_DIR",
                "MEMORYMASTER_LOG_DIR")


@contextlib.contextmanager
def _hermetic_environment(tmp: Path) -> Any:
    saved = dict(os.environ)
    for name in list(os.environ):
        if name.startswith(LIVE_CONFIG_PREFIXES) or name in LIVE_CONFIG_NAMES:
            del os.environ[name]
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    os.environ.update(HOME=str(home), USERPROFILE=str(home), MEMORYMASTER_LLM_RERANK="0",
                      MEMORYMASTER_RECALL_RERANK="0", MEMORYMASTER_DECISIONS_DB=str(tmp / "decisions.db"))
    for name in SCRATCH_DIRS:
        os.environ[name] = str(tmp / name.removeprefix("MEMORYMASTER_").lower())
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _is_loopback(host: object) -> bool:
    if not isinstance(host, str):
        return True  # AF_UNIX paths and similar are local
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False  # an unresolved name is an outbound attempt


@contextlib.contextmanager
def _egress_guard(attempts: list[str]) -> Any:
    """Refuse and record every non-loopback connection: provider_calls is measured, not assumed."""
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def _check(address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else address
        if not _is_loopback(host):
            attempts.append(str(host))
            raise ConnectionRefusedError(f"outbound connection blocked by autoresearch_perf_gate: {host}")

    def connect(sock: socket.socket, address: Any) -> None:
        _check(address)
        return original_connect(sock, address)

    def connect_ex(sock: socket.socket, address: Any) -> int:
        _check(address)
        return original_connect_ex(sock, address)

    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex = original_connect, original_connect_ex


def _median(samples: list[dict[str, Any]], *path: str) -> float:
    values: list[float] = []
    for sample in samples:
        value: Any = sample
        for key in path:
            value = value[key]
        values.append(float(value))
    return statistics.median(values)


def _aggregate(samples: list[dict[str, Any]], expected_claims: int, provider_calls: int) -> dict[str, Any]:
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
        "provider_calls": provider_calls,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims", type=int, default=80)
    parser.add_argument("--queries", type=int, default=30)
    parser.add_argument("--cycles", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-provider-calls", type=int, default=0,
                        help="outbound connections allowed; any more fails the gate")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if min(args.claims, args.queries, args.repeats) <= 0 or args.cycles < 0:
        raise SystemExit("claims, queries, and repeats must be positive; cycles cannot be negative")
    perf_smoke = _load_perf_smoke()
    samples: list[dict[str, Any]] = []
    attempts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mm-autoresearch-perf-") as tmp,             _hermetic_environment(Path(tmp)), _egress_guard(attempts):
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
    result = _aggregate(samples, args.claims, len(attempts))
    violations = []
    if result["provider_calls"] > args.max_provider_calls:
        violations.append(f"provider_calls {result['provider_calls']} > max {args.max_provider_calls}")
    result.update(verdict="fail" if violations else "pass", violations=violations)
    print(json.dumps(result, sort_keys=True))
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
