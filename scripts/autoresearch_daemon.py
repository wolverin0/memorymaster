"""Autonomous autoresearch daemon — continuously improves memorymaster.

Runs 3 improvement loops in rotation:
  1. Code quality: ruff extended rules, fix issues
  2. Test coverage: find untested code paths, write tests
  3. Performance: profile operations, optimize bottlenecks

Each iteration: measure → change → verify → commit or revert.
Runs until killed or max_hours reached.

Usage:
    python scripts/autoresearch_daemon.py                    # run forever
    python scripts/autoresearch_daemon.py --max-hours 6      # run for 6 hours
    python scripts/autoresearch_daemon.py --mode quality      # only code quality
    nohup python scripts/autoresearch_daemon.py --max-hours 8 > autoresearch.log 2>&1 &
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
os.chdir(str(ROOT))

LOG_FILE = ROOT / "autoresearch_results.jsonl"


def log_result(mode: str, iteration: int, action: str, result: str, metric_before: str = "", metric_after: str = ""):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "iteration": iteration,
        "action": action,
        "result": result,
        "metric_before": metric_before,
        "metric_after": metric_after,
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[{mode}:{iteration}] {action} → {result}")


def run(cmd: str, timeout: int = 120) -> tuple[int, str]:
    """Run a shell command, return (returncode, output).

    Accepts a shell-string so the daemon can keep using redirections
    (``2>/dev/null``, ``2>&1``) and pipes. Callers MUST NOT build this
    string with untrusted input — use :func:`run_argv` for anything that
    interpolates user-provided values.
    """
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT)
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "timeout"


def run_argv(argv: list[str], timeout: int = 120) -> tuple[int, str]:
    """Run a command as an argv list with ``shell=False`` — injection-safe.

    Use this whenever any element comes from variable content. No shell is
    spawned, so characters like ``;``, ``&``, ``|``, backticks, and quotes
    are passed literally to the child process.
    """
    try:
        result = subprocess.run(
            argv, shell=False, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT)
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "timeout"


def git_revert():
    """Revert all uncommitted changes."""
    run_argv(["git", "checkout", "--", "."])


def git_commit(message: str) -> bool:
    """Stage and commit all changes. Message is passed as argv — safe from
    shell injection regardless of content."""
    run_argv(["git", "add", "-A"])
    rc, _ = run_argv(["git", "commit", "-m", message])
    return rc == 0


def guard_tests() -> bool:
    """Run guard tests — must pass before committing."""
    rc, output = run("python -m pytest tests/test_sqlite_core.py tests/test_service_coverage.py -q --tb=no", timeout=300)
    return rc == 0 and "failed" not in output


# ─── CODE QUALITY LOOP ───

def quality_iteration(i: int) -> bool:
    """One iteration of code quality improvement."""
    # Measure
    rc, output = run("ruff check memorymaster/ --extend-select C901,B,SIM --statistics 2>&1")
    if "All checks passed" in output or rc == 0:
        log_result("quality", i, "measure", "all_clean")
        return False  # nothing to fix

    metric_before = output.split("\n")[-1] if output else "?"

    # Auto-fix what ruff can
    rc, _ = run("ruff check memorymaster/ --extend-select C901,B,SIM --fix 2>&1")

    # Re-measure
    rc2, output2 = run("ruff check memorymaster/ --extend-select C901,B,SIM --statistics 2>&1")
    metric_after = output2.split("\n")[-1] if output2 else "?"

    if metric_before == metric_after:
        git_revert()
        log_result("quality", i, "no_improvement", "reverted", metric_before, metric_after)
        return True

    # Guard
    if not guard_tests():
        git_revert()
        log_result("quality", i, "guard_failed", "reverted", metric_before, metric_after)
        return True

    git_commit(f"fix: autoresearch quality iteration {i}")
    log_result("quality", i, "fixed", "committed", metric_before, metric_after)
    return True


# ─── PERFORMANCE LOOP ───

def perf_iteration(i: int) -> bool:
    """One iteration of performance optimization."""
    # Profile query
    rc, output = run(
        'python -c "'
        "import time; "
        "from memorymaster.service import MemoryService; "
        "from pathlib import Path; "
        "svc = MemoryService(db_target='memorymaster.db', workspace_root=Path('.')); "
        "t=time.perf_counter(); "
        "svc.query('test query', limit=10); "
        "print(f'{(time.perf_counter()-t)*1000:.0f}')"
        '"',
        timeout=30,
    )
    try:
        ms = int(output.strip().split("\n")[-1])
    except (ValueError, IndexError):
        log_result("perf", i, "profile_failed", output[:100])
        return False

    log_result("perf", i, "profiled", f"query={ms}ms")

    if ms < 50:
        log_result("perf", i, "already_fast", f"{ms}ms < 50ms target")
        return False

    return True  # Further optimization needed but requires code analysis


# ─── VALIDATION CYCLE LOOP ───

def validation_iteration(i: int) -> bool:
    """Run a validation cycle to process pending claims."""
    rc, output = run("python -m memorymaster run-cycle 2>/dev/null", timeout=60)
    if rc != 0:
        log_result("validation", i, "cycle_failed", output[:200])
        return False

    try:
        data = json.loads(output)
        validator = data.get("validator", {})
        processed = validator.get("processed", 0)
        confirmed = validator.get("confirmed", 0)
        log_result("validation", i, "cycle_complete", f"processed={processed} confirmed={confirmed}")
        return processed > 0
    except json.JSONDecodeError:
        log_result("validation", i, "parse_error", output[:100])
        return False


# ─── ENTITY EXTRACTION LOOP ───

def entity_iteration(i: int) -> bool:
    """Extract entities from a batch of claims."""
    rc, output = run("python -m memorymaster extract-entities --limit 10 --status confirmed 2>/dev/null", timeout=120)
    if rc != 0:
        log_result("entities", i, "extraction_failed", output[:200])
        return False

    log_result("entities", i, "extracted", output.split("\n")[-1][:100] if output else "empty")
    return True


# ─── MAIN DAEMON ───

def main():
    parser = argparse.ArgumentParser(description="Autoresearch daemon")
    parser.add_argument("--max-hours", type=float, default=0, help="Max hours to run (0=forever)")
    parser.add_argument("--mode", default="all", choices=["all", "quality", "perf", "validation", "entities"])
    parser.add_argument("--interval", type=int, default=30, help="Seconds between iterations")
    args = parser.parse_args()

    start = time.monotonic()
    max_seconds = args.max_hours * 3600 if args.max_hours > 0 else float("inf")
    iteration = 0

    print(f"Autoresearch daemon started: mode={args.mode}, max_hours={args.max_hours or 'infinite'}")
    print(f"Log: {LOG_FILE}")

    modes = ["quality", "validation", "entities"] if args.mode == "all" else [args.mode]

    while True:
        elapsed = time.monotonic() - start
        if elapsed > max_seconds:
            print(f"Max time reached ({args.max_hours}h). Stopping.")
            break

        iteration += 1
        mode = modes[iteration % len(modes)]

        try:
            if mode == "quality":
                quality_iteration(iteration)
            elif mode == "perf":
                perf_iteration(iteration)
            elif mode == "validation":
                validation_iteration(iteration)
            elif mode == "entities":
                entity_iteration(iteration)
        except Exception as exc:
            log_result(mode, iteration, "error", str(exc)[:200])

        time.sleep(args.interval)

    print(f"Autoresearch daemon finished. {iteration} iterations in {elapsed/3600:.1f}h")


if __name__ == "__main__":
    main()
