"""CLI handlers — `integrity`, `repair-fk`, `qdrant-reconcile` + `drain-spool`
subcommands (P1 spec §2.5/§2.6/§2.7/§2.4).

Lives in its own module because `cli_handlers_basic.py` is already past the
800-LOC budget. Registered into COMMAND_HANDLERS by `cli.py`.
"""
from __future__ import annotations

import argparse
import json
import time

from memorymaster.surfaces.cli_helpers import _json_envelope
from memorymaster.govern.jobs import fk_repair, integrity, qdrant_reconcile, spool_drain


def _handle_integrity(
    args: argparse.Namespace,
    service,
    parser: argparse.ArgumentParser,
    effective_db: str,
) -> int:
    """Run integrity operations against the SQLite DB.

    No flags → the full steward phase (daily/weekly throttles apply, exactly
    as `run-cycle` would run it). Explicit flags force the named phases,
    bypassing throttles — that is the operator/runbook path (e.g. the
    hermes-sync checkpoint piggyback, the supervised rollout TRUNCATE).
    """
    store = service.store
    db_path = getattr(store, "db_path", None)
    t0 = time.perf_counter()

    explicit = args.checkpoint or args.quick_check or args.fk_check or args.vacuum_snapshot
    if db_path is None:
        data: dict[str, object] = {"skipped": "not_sqlite"}
    elif args.status:
        data = integrity.status(store, db_path)
    elif explicit:
        data = {}
        if args.checkpoint:
            data["checkpoint"] = integrity.checkpoint(store, db_path)
        if args.quick_check:
            data["quick_check"] = integrity.quick_check(store, db_path, force=True)
        if args.fk_check:
            data["fk_check"] = integrity.fk_check(store, db_path, force=True)
        if args.vacuum_snapshot:
            data["vacuum_snapshot"] = integrity.vacuum_snapshot(store, db_path, force=True)
    else:
        data = integrity.run(store)

    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(data, query_ms=elapsed_ms))
    else:
        print(json.dumps(data, indent=2))

    quick = data.get("quick_check")
    if isinstance(quick, dict) and quick.get("ok") is False:
        return 1
    return 0


def _handle_repair_fk(
    args: argparse.Namespace,
    service,
    parser: argparse.ArgumentParser,
    effective_db: str,
) -> int:
    """Repair orphan FK rows (spec §2.6, F10's 401 recovery-collateral rows).

    Dry-run by default — reports the (table, parent) grouping and the
    disposal plan without mutating anything. `--apply` quarantines every
    orphan row to JSONL and repairs in ONE transaction. Run `--apply` ONCE
    supervised on the live DB after merge; thereafter the daily integrity
    fk_check phase only detects.
    """
    store = service.store
    db_path = getattr(store, "db_path", None)
    t0 = time.perf_counter()

    if db_path is None:
        data: dict[str, object] = {"skipped": "not_sqlite"}
    else:
        data = fk_repair.run(
            store,
            apply=args.apply,
            quarantine_dir=args.quarantine_dir or None,
        )

    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(data, query_ms=elapsed_ms))
    else:
        print(json.dumps(data, indent=2))

    if data.get("error") or data.get("ok") is False:
        return 1
    return 0


def _handle_qdrant_reconcile(
    args: argparse.Namespace,
    service,
    parser: argparse.ArgumentParser,
    effective_db: str,
) -> int:
    """Reconcile SQLite truth vs Qdrant point count (spec §2.7).

    The operator CLI bypasses the daily throttle (`force=True`) — running the
    command IS the operator's intent. `--full` forces a sync_all + orphan
    delete even when drift is under the threshold. Skips cleanly when
    QDRANT_URL is unset (most dev machines).
    """
    t0 = time.perf_counter()
    data = qdrant_reconcile.run(
        service.store,
        service.qdrant,
        force=True,
        full=args.full,
        threshold=args.threshold,
    )

    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(data, query_ms=elapsed_ms))
    else:
        print(json.dumps(data, indent=2))

    if data.get("error"):
        return 1
    return 0


def _handle_drain_spool(
    args: argparse.Namespace,
    service,
    parser: argparse.ArgumentParser,
    effective_db: str,
) -> int:
    """One-shot spool drain (spec §2.4 — same replay the steward cycle runs).

    This is also the §5 rollback path: after `MEMORYMASTER_WAL_DISCIPLINE=0`
    any spool residue drains here so no write is ever stranded. Replays go
    through `svc.ingest` / `store_verbatim` / `record_accesses_batch` /
    `FeedbackTracker` — sensitivity filter and idempotent dedup apply.
    """
    t0 = time.perf_counter()
    data = spool_drain.run(service)

    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(data, query_ms=elapsed_ms))
    else:
        print(json.dumps(data, indent=2))

    if data.get("error"):
        return 1
    return 0
