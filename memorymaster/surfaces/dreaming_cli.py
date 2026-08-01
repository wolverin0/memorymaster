"""CLI handlers for native Dreaming execution and read-only health."""

from __future__ import annotations

import json

from memorymaster.core.capture_control import capture_state_path
from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.providers import GLMConsolidator, create_dream_extractor
from memorymaster.dreaming.worker import DreamWorker


def handle_dream_run(args, service, parser, effective_db) -> int:
    capture_result = None
    if hasattr(service, "store"):
        from memorymaster.capture.worker import run_capture_worker

        service.init_db()
        capture_result = run_capture_worker(service, limit=25)
    ledger = DreamLedger(capture_state_path())
    worker = DreamWorker(ledger, service, create_dream_extractor(), GLMConsolidator())
    result = worker.run(
        apply_candidates=bool(args.apply_candidates),
        scope=(args.scope or None),
        max_sessions=args.max_sessions,
    )
    if capture_result is not None:
        result["capture"] = {
            "leased": capture_result.leased,
            "completed": capture_result.completed,
            "retryable": capture_result.retryable,
            "blocked": capture_result.blocked,
            "errors": capture_result.errors,
        }
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False))
    else:
        mode = "APPLY CANDIDATES" if args.apply_candidates else "SHADOW"
        print(
            f"dream-run [{mode}]: extracted={result.get('extracted', 0)} "
            f"consolidated={result.get('consolidated', 0)} "
            f"applied={result.get('applied', 0)} errors={result.get('errors', 0)}"
        )
    return 0 if result.get("ok") and not result.get("errors") else 1


def handle_dream_status(args, service, parser, effective_db) -> int:
    status = {"ok": True, **DreamLedger.read_status(capture_state_path())}
    if args.json_output:
        print(json.dumps(status, ensure_ascii=False))
    else:
        provider_window = status["provider_window"]
        print(
            "dream-status: "
            f"queue={status['queue']} warnings={status['warnings']} "
            f"hook_errors={status['hook_errors']} "
            f"providers_{provider_window['hours']}h={provider_window['providers']} "
            f"providers_lifetime={status['providers_lifetime']}"
        )
    return 0
