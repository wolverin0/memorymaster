#!/usr/bin/env python3
"""Local bulk gate: the whole merge check runs here, on this host, in one go.

Replaces the GitHub Actions pipeline (removed 2026-10-25 by operator order: the
hosted runners took up to two hours per Windows job and blocked nothing, every
merge of the last weeks was an admin merge over a queued matrix). A PR is ready
when this script exits 0 on the branch; nothing else gates a merge.

Steps, in order, stop at the first failure:
  1. generate_release_truth.py --check     generated release metadata is current
  2. check_contract_witness.py --base      behaviour change touched its witness test
  3. check_probes_frozen.py                frozen probe thresholds untouched
  4. check_branch_freshness.py             branch is not behind its base
  5. smoke_mcp_transport.py --transport stdio   MCP handshake over stdio
  6. pytest tests/ -m "not ml" -n <workers>     full non-ML suite (parallel)
  7. pytest tests/ -m ml                   only with --ml (torch/Qdrant, isolated)

Usage: python scripts/bulk_check.py [--base origin/main] [--workers 2] [--fast] [--ml]
  --fast  runs steps 1-5 only (seconds); the suite still has to run before merge.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FAST_GATES = (
    ("release-truth", ["scripts/generate_release_truth.py", "--check"]),
    ("contract-witness", ["scripts/check_contract_witness.py", "--base", "{base}"]),
    ("probes-frozen", ["scripts/check_probes_frozen.py"]),
    ("branch-freshness", ["scripts/check_branch_freshness.py"]),
    ("mcp-stdio-smoke", ["scripts/smoke_mcp_transport.py", "--transport", "stdio"]),
)
SUITE = ["-m", "pytest", "tests/", "-m", "not ml", "-q", "--tb=short", "-n", "{workers}", "-p", "no:cacheprovider"]
ML_SUITE = ["-m", "pytest", "tests/", "-m", "ml", "-q", "--tb=short", "-p", "no:cacheprovider"]


def _run(name: str, argv: list[str]) -> int:
    started = time.monotonic()
    print(f"\n== {name}: {' '.join(argv)}", flush=True)
    code = subprocess.run([sys.executable, *argv], cwd=ROOT, check=False).returncode
    print(f"== {name}: {'ok' if code == 0 else f'FAIL rc={code}'} ({time.monotonic() - started:.0f}s)", flush=True)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--workers", type=int, default=2, help="pytest-xdist workers for the non-ML suite")
    parser.add_argument("--fast", action="store_true", help="fast gates only; skip the test suites")
    parser.add_argument("--ml", action="store_true", help="also run the isolated ML suite")
    args = parser.parse_args(argv)

    steps: list[tuple[str, list[str]]] = [
        (name, [part.format(base=args.base) for part in argv_]) for name, argv_ in FAST_GATES
    ]
    if not args.fast:
        # Workers share tests/.tmp_cases and never prune it (tests/conftest.py); start clean.
        for shared in (".tmp_cases", ".tmp_pytest"):
            shutil.rmtree(ROOT / shared, ignore_errors=True)
        steps.append(("suite-not-ml", [part.format(workers=args.workers) for part in SUITE]))
        if args.ml:
            steps.append(("suite-ml", list(ML_SUITE)))
    for name, step_argv in steps:
        code = _run(name, step_argv)
        if code != 0:
            print(f"\nbulk_check: FAIL at {name}", flush=True)
            return code
    print("\nbulk_check: ok" + (" (fast gates only)" if args.fast else ""), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
