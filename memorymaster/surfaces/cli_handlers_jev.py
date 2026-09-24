"""CLI wiring for operating Jev decisions (4.9.0).

``jev-status`` (metrics JSON), ``jev-review-queue`` (weekly review queue),
``jev-export`` (training/OPE JSONL with a time split) and ``jev-prune`` (state
retention) work on the decisions ledger alone; reads open it read-only and never
create it.  ``jev-revalidate`` and ``jev-release-held`` lazily import the S1
revalidation job and the Dreaming ``held`` module, so this CLI loads without them
and reports a clear error when a build does not include them.
"""
from __future__ import annotations

import importlib
import json
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timezone
from typing import Any

REVALIDATION_MODULE = "memorymaster.govern.jobs.revalidation"
HELD_MODULE = "memorymaster.dreaming.held"
LEDGER_COMMANDS = ("jev-status", "jev-review-queue", "jev-export", "jev-prune", "jev-release-held")


class MissingJevModule(RuntimeError):
    """A lazily imported Jev module is not part of this build."""


def register_jev_parsers(sub: Any) -> None:
    status = sub.add_parser("jev-status", help="Print Jev decision metrics per surface as JSON (read-only)")
    status.add_argument("--days", type=int, default=7, help="Metrics window in days (default 7)")

    revalidate = sub.add_parser("jev-revalidate", help="Ask Jev to re-confirm stale claims (S1 revalidation job)")
    revalidate.add_argument("--limit", type=int, default=50, help="Maximum stale claims to examine (default 50)")
    revalidate.add_argument("--max-usd", type=float, default=None,
                            help="Spend ceiling for this run (default: MEMORYMASTER_JEV_DAILY_USD_CAP)")
    revalidate.add_argument("--backfill", action="store_true", help="Work through the historical stale backlog")

    release = sub.add_parser("jev-release-held", help="Release a Dreaming candidate that Jev held")
    release.add_argument("--capture-id", type=int, required=True, help="Dreaming capture id")
    release.add_argument("--candidate-id", required=True, help="Candidate id inside that capture")

    export = sub.add_parser("jev-export", help="Export decisions as training/OPE JSONL (read-only)")
    export.add_argument("--output", required=True, help="JSONL file to write")
    export.add_argument("--since", default=None, help="Only decisions at or after this ISO-8601 time")
    export.add_argument("--until", default=None, help="Only decisions at or before this ISO-8601 time")
    export.add_argument("--split-at", default=None,
                        help="Time split: decisions before this ISO-8601 time are 'train', later ones 'test'")
    export.add_argument("--include-state", action="store_true", help="Include the redacted request state")

    queue = sub.add_parser("jev-review-queue", help="Print this week's operator review queue as JSON (read-only)")
    queue.add_argument("--size", type=int, default=20, help="Queue size (default 20)")
    queue.add_argument("--days", type=int, default=7, help="Look-back window in days (default 7)")

    prune = sub.add_parser("jev-prune", help="Null redacted request text older than the retention window")
    prune.add_argument("--retention-days", type=int, default=None,
                       help="Override MEMORYMASTER_DECISIONS_STATE_RETENTION_DAYS")


def _parse_time(value: str | None, flag: str) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError as exc:
        raise ValueError(f"invalid {flag}: expected an ISO-8601 time") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _print(payload: Any) -> None:
    if is_dataclass(payload) and not isinstance(payload, type):
        payload = asdict(payload)
    # ASCII escapes: piped Windows stdout is cp1252 and claim text is not.
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2, default=str))


def _positive(value: int, flag: str) -> int:
    if value < 1:
        raise ValueError(f"{flag} must be at least 1")
    return value


def _lazy(module_name: str) -> Any:
    """Import a module built by another track; a missing module is a clear error, not a traceback."""
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing and (module_name == missing or module_name.startswith(missing + ".")):
            raise MissingJevModule(f"{module_name} is not available in this build of memorymaster") from None
        raise


def _missing(exc: MissingJevModule) -> int:
    print(f"error: {exc}")
    return 2


def handle_jev_status(args, service, parser, effective_db) -> int:
    from memorymaster.surfaces.jev_review import metrics_payload, read_ledger

    _print(metrics_payload(read_ledger(), days=_positive(args.days, "--days")))
    return 0


def handle_jev_review_queue(args, service, parser, effective_db) -> int:
    from memorymaster.surfaces.jev_review import read_ledger, select_review_queue

    items = select_review_queue(read_ledger(), days=_positive(args.days, "--days"),
                                size=_positive(args.size, "--size"))
    _print({"ok": True, "items": items})
    return 0


def handle_jev_export(args, service, parser, effective_db) -> int:
    from memorymaster.decisions.export import export_jsonl
    from memorymaster.surfaces.jev_review import read_ledger, refuse_ledger_output

    since, until = _parse_time(args.since, "--since"), _parse_time(args.until, "--until")
    split_at = _parse_time(args.split_at, "--split-at")
    ledger = read_ledger()
    rows = export_jsonl(ledger, refuse_ledger_output(ledger, args.output), since=since, until=until,
                        split_at=split_at, include_state=bool(args.include_state))
    _print({"ok": True, "rows": rows, "output": str(args.output)})
    return 0


def handle_jev_prune(args, service, parser, effective_db) -> int:
    from memorymaster.decisions.config import DecisionConfig
    from memorymaster.decisions.ledger import prune_job

    config = DecisionConfig.from_env()
    if args.retention_days is not None:
        config = replace(config, state_retention_days=_positive(args.retention_days, "--retention-days"))
    _print({"ok": True, "pruned": prune_job(config), "retention_days": config.state_retention_days})
    return 0


def handle_jev_revalidate(args, service, parser, effective_db) -> int:
    from memorymaster.decisions.config import DecisionConfig

    try:
        job = _lazy(REVALIDATION_MODULE)
    except MissingJevModule as exc:
        return _missing(exc)
    max_usd = args.max_usd if args.max_usd is not None else DecisionConfig.from_env().daily_usd_cap
    if max_usd < 0:
        raise ValueError("--max-usd must not be negative")
    result = job.run(service, limit=_positive(args.limit, "--limit"), max_usd=max_usd,
                     backfill=bool(args.backfill))
    _print(result)
    return 0


def handle_jev_release_held(args, service, parser, effective_db) -> int:
    from memorymaster.core.capture_control import capture_state_path

    try:
        held = _lazy(HELD_MODULE)
    except MissingJevModule as exc:
        return _missing(exc)
    result = held.release(capture_state_path(), capture_id=args.capture_id, candidate_id=args.candidate_id,
                          actor="operator")
    _print(result)
    return 0


JEV_COMMAND_HANDLERS = {
    "jev-status": handle_jev_status,
    "jev-revalidate": handle_jev_revalidate,
    "jev-release-held": handle_jev_release_held,
    "jev-export": handle_jev_export,
    "jev-review-queue": handle_jev_review_queue,
    "jev-prune": handle_jev_prune,
}

__all__ = ["JEV_COMMAND_HANDLERS", "LEDGER_COMMANDS", "register_jev_parsers"]
