#!/usr/bin/env python3
"""Drain the Dreaming capture queue in bounded batches (T-2123).

Runs the same extract + consolidate + apply pass as the MemoryMaster-Dreaming task, ``--batch-size`` captures
at a time, and stops by itself. It never runs unless asked: ``--plan`` only reads and prints.

Stops (``stop_reason``), checked before and after every batch:
  under_threshold    queue <= --stop-below captures AND oldest idle age <= --max-age-h (the health probe's limits)
  breaker_open       the consolidation circuit breaker opened in the batch
  daily_budget       an extract/consolidate daily call or token cap deferred work (rerun after 00:00 UTC)
  no_progress        a batch extracted, consolidated and applied nothing
  run_failed         the batch raised, was not ok, or lost more captures to errors than it processed
  worker_busy        another worker holds the dream-worker lease
  latency_guard_skip the Beat latency guard says interactive hooks are slow right now
  max_batches / max_minutes   the bounds of this invocation

Run it under the heavy-job semaphore and never by hand against a busy host:

    node wezbridge/scripts/heavy-slot.cjs run --label t2123-drain --card T-2123 --timeout-ms 14400000 -- \\
        python scripts/drain_capture_queue.py --db memorymaster.db --workspace .

Spend knobs are raised for this process only (never lowered, nothing persisted): the standing daily caps
(40/200 extract calls, 2M input tokens per provider) were sized for ~300 captures a day; draining a backlog on
top of that intake needs ``--extract-calls-daily`` and ``--max-input-tokens-daily``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:  # `python scripts/x.py` puts scripts/, not the repo, on sys.path
    sys.path.insert(0, str(REPO))

# Same limits as the health probe's ingest.capture_queue (scripts/memory_health_probe.py CONFIG).
STOP_BELOW = 1000
MAX_AGE_H = 168.0

# Measured 2026-11-03: ~12.5K input tokens per consolidated capture, ~1.1 extract calls per capture.
# To clear ~3000 captures in about a week on top of ~300 a day of intake and ~100 a day the Beat's Claude
# extractor already takes: ~650 captures a day through this path = ~8M tokens and ~700 extract calls.
DEFAULT_EXTRACT_CALLS_DAILY = 700
DEFAULT_MAX_INPUT_TOKENS_DAILY = 8_000_000

# Same provider contract as the MemoryMaster-Dreaming scheduled task.
DEFAULT_EXTRACT_PROVIDER = "gemini"
DEFAULT_EXTRACT_MODEL = "gemini-flash-lite-latest"
DEFAULT_CONSOLIDATE_MODEL = "gemini-3.8-flash-low"


def _age_h(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    stamp = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    return round((now - stamp).total_seconds() / 3600, 1)


def queue_state(ledger, now: datetime) -> dict:
    queue = ledger.drainable_queue()
    return {"waiting": queue["waiting"], "oldest_age_h": _age_h(queue["oldest_last_activity_at"], now)}


def _under(state: dict, stop_below: int, max_age_h: float) -> bool:
    age = state["oldest_age_h"]
    return state["waiting"] <= stop_below and (age is None or age <= max_age_h)


def _batch_stop(result: dict) -> str | None:
    """Why a finished batch ends the drain, or None to continue."""
    if result.get("reason") == "worker_busy":
        return "worker_busy"
    if result.get("consolidate_breaker"):
        return "breaker_open"
    if int(result.get("deferred_extract_budget") or 0) or int(result.get("deferred_consolidate_budget") or 0):
        return "daily_budget"
    done = int(result.get("extracted") or 0) + int(result.get("consolidated") or 0) + int(result.get("applied") or 0)
    if not result.get("ok", True) or int(result.get("errors") or 0) > max(done, 0):
        return "run_failed"
    if done == 0:
        return "no_progress"
    return None


def drain(
    *,
    state: Callable[[], dict],
    run_batch: Callable[[], dict],
    guard: Callable[[], str] = lambda: "allow",
    stop_below: int = STOP_BELOW,
    max_age_h: float = MAX_AGE_H,
    max_batches: int = 40,
    max_minutes: float = 240.0,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[dict], None] = lambda event: None,
) -> dict:
    """Run batches until a stop condition; every collaborator is injected so the loop is testable."""
    started = clock()
    before = state()
    totals = {"extracted": 0, "consolidated": 0, "applied": 0, "errors": 0}
    batches = 0
    reason = ""
    current = before
    while True:
        if _under(current, stop_below, max_age_h):
            reason = "under_threshold"
        elif batches >= max_batches:
            reason = "max_batches"
        elif (clock() - started) / 60 >= max_minutes:
            reason = "max_minutes"
        elif guard() == "skip":
            reason = "latency_guard_skip"
        if reason:
            break
        try:
            result = run_batch()
        except Exception as exc:  # noqa: BLE001 - a crashed batch ends the drain with its reason, never loops
            reason = f"run_failed:{type(exc).__name__}"
            break
        batches += 1
        for key in totals:
            totals[key] += int(result.get(key) or 0)
        current = state()
        log({"batch": batches, "result": {k: result.get(k) for k in (*totals, "reason", "consolidate_breaker",
                                                                      "deferred_extract_budget",
                                                                      "deferred_consolidate_budget")},
             "queue": current})
        reason = _batch_stop(result) or ""
        if reason and _under(current, stop_below, max_age_h):
            reason = "under_threshold"
        if reason:
            break
    return {"stop_reason": reason, "batches": batches, "totals": totals, "before": before, "after": current,
            "minutes": round((clock() - started) / 60, 1)}


def _raise_cap(name: str, wanted: int) -> None:
    """Raise a daily cap for this process only; never lower what the operator already set."""
    try:
        current = int(os.environ.get(name, "0"))
    except ValueError:
        current = 0
    if current > 0 and wanted > current:  # 0 or negative means "disabled" for the token cap: leave it
        os.environ[name] = str(wanted)
    elif name not in os.environ:
        os.environ[name] = str(wanted)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--plan", action="store_true", help="read-only: print the queue, the caps and the stop rules, call nothing")
    parser.add_argument("--batch-size", type=int, default=20, help="captures per batch (DreamConfig.max_sessions)")
    parser.add_argument("--max-batches", type=int, default=40)
    parser.add_argument("--max-minutes", type=float, default=240.0)
    parser.add_argument("--stop-below", type=int, default=STOP_BELOW)
    parser.add_argument("--max-age-h", type=float, default=MAX_AGE_H)
    parser.add_argument("--extract-calls-daily", type=int, default=DEFAULT_EXTRACT_CALLS_DAILY)
    parser.add_argument("--max-input-tokens-daily", type=int, default=DEFAULT_MAX_INPUT_TOKENS_DAILY)
    parser.add_argument("--extract-provider", default=DEFAULT_EXTRACT_PROVIDER)
    parser.add_argument("--extract-model", default=DEFAULT_EXTRACT_MODEL)
    parser.add_argument("--consolidate-model", default=DEFAULT_CONSOLIDATE_MODEL)
    parser.add_argument("--no-guard", action="store_true", help="ignore the Beat latency guard (default: stop on skip)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    from dataclasses import replace

    from memorymaster.core.capture_control import capture_state_path
    from memorymaster.dreaming import latency_guard
    from memorymaster.dreaming.ledger import DreamLedger
    from memorymaster.dreaming.worker import DreamConfig, run_dream
    from memorymaster.surfaces.scheduled_task import _apply_dream_provider_contract

    _apply_dream_provider_contract(argparse.Namespace(
        extract_provider=args.extract_provider, extract_model=args.extract_model, extract_variant="",
        consolidate_model=args.consolidate_model, consolidate_variant="", clear_provider_variants=True))
    _raise_cap("MEMORYMASTER_DREAM_MAX_EXTRACT_CALLS_DAILY", args.extract_calls_daily)
    _raise_cap("MEMORYMASTER_DREAM_MAX_INPUT_TOKENS_DAILY", args.max_input_tokens_daily)
    ledger = DreamLedger(capture_state_path())

    def state() -> dict:
        return queue_state(ledger, datetime.now(timezone.utc))

    if args.plan:
        config = DreamConfig.from_env()
        print(json.dumps({
            "queue": state(), "stop_below": args.stop_below, "max_age_h": args.max_age_h,
            "batch_size": args.batch_size, "max_batches": args.max_batches,
            "extract": f"{args.extract_provider}/{args.extract_model}", "consolidate_model": args.consolidate_model,
            "caps_this_process": {"extract_calls_daily": config.max_extract_calls_daily,
                                  "consolidate_calls_daily": config.max_consolidate_calls_daily,
                                  "input_tokens_daily": config.max_input_tokens_daily},
            "guard_now": None if args.no_guard else latency_guard.evaluate(record=False).action,
        }, indent=2))
        return 0

    def run_batch() -> dict:
        config = replace(DreamConfig.from_env(), max_sessions=args.batch_size)
        return run_dream(args.db, args.workspace, apply_candidates=True, config=config)

    report = drain(
        state=state, run_batch=run_batch,
        guard=(lambda: "allow") if args.no_guard else (lambda: latency_guard.evaluate(record=False).action),
        stop_below=args.stop_below, max_age_h=args.max_age_h,
        max_batches=args.max_batches, max_minutes=args.max_minutes,
        log=lambda event: print(json.dumps(event), flush=True),
    )
    print(json.dumps(report, indent=2))
    return 0 if report["stop_reason"] == "under_threshold" else 2


if __name__ == "__main__":
    raise SystemExit(main())
