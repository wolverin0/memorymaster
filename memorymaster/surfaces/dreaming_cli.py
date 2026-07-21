"""CLI handlers for native Dreaming execution and read-only health."""

from __future__ import annotations

import json

from memorymaster.core.capture_control import capture_state_path
from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.providers import GLMConsolidator, GeminiExtractor
from memorymaster.dreaming.worker import DreamWorker


def handle_dream_run(args, service, parser, effective_db) -> int:
    ledger = DreamLedger(capture_state_path())
    worker = DreamWorker(ledger, service, GeminiExtractor(), GLMConsolidator())
    result = worker.run(
        apply_candidates=bool(args.apply_candidates),
        scope=(args.scope or None),
        max_sessions=args.max_sessions,
    )
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False))
    else:
        mode = "APPLY CANDIDATES" if args.apply_candidates else "SHADOW"
        print(
            f"dream-run [{mode}]: extracted={result.get('extracted', 0)} "
            f"consolidated={result.get('consolidated', 0)} "
            f"applied={result.get('applied', 0)} errors={result.get('errors', 0)}"
        )
    return 0 if result.get("ok") else 1


def handle_dream_status(args, service, parser, effective_db) -> int:
    status = {"ok": True, **DreamLedger.read_status(capture_state_path())}
    if args.json_output:
        print(json.dumps(status, ensure_ascii=False))
    else:
        print(
            "dream-status: "
            f"queue={status['queue']} warnings={status['warnings']} "
            f"hook_errors={status['hook_errors']}"
        )
    return 0
