"""CLI registration and dispatch for local Workflow Intelligence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memorymaster.workflow_intelligence.candidates import (
    refresh_candidates,
    review_candidate,
    write_proposal,
)
from memorymaster.workflow_intelligence.classification import classify_pending
from memorymaster.workflow_intelligence.report import build_report, write_report
from memorymaster.workflow_intelligence.redaction import public_excerpt
from memorymaster.workflow_intelligence.scanner import WorkflowScanner
from memorymaster.workflow_intelligence.storage import WorkflowStore, utc_now


def register_workflow_parser(subparsers: Any) -> None:
    workflow = subparsers.add_parser(
        "workflow", help="Analyze retained coding-agent trajectories in a rebuildable sidecar"
    )
    commands = workflow.add_subparsers(dest="workflow_command", required=True)
    scan = commands.add_parser("scan", help="Index sources and optionally deep-parse sessions")
    scan.add_argument("--deep", choices=["none", "human", "selected"], default="none")
    scan.add_argument("--session", action="append", default=[], help="External session id (repeatable)")
    inspect = commands.add_parser("inspect", help="Inspect one normalized session")
    inspect.add_argument("session_id")
    classify = commands.add_parser("classify", help="Opt in to bounded LLM classification")
    classify.add_argument("--limit", type=int, default=50)
    report = commands.add_parser("report", help="Write local self-contained JSON and HTML")
    report.add_argument("--scope", default=None)
    report.add_argument("--since", default=None)
    report.add_argument("--output", default=None)
    candidates = commands.add_parser("candidates", help="List deterministic intervention candidates")
    candidates.add_argument("--status", choices=["watch", "proposed", "reviewed"], default=None)
    review = commands.add_parser("review", help="Record a human decision without promotion")
    review.add_argument("candidate_id")
    review.add_argument(
        "--decision", choices=["accept_pattern", "reject_noise", "watch", "relabel"], required=True
    )
    review.add_argument("--rationale", default="")
    proposal = commands.add_parser("proposal", help="Export an inert intervention proposal")
    proposal.add_argument("candidate_id")
    proposal.add_argument("--output", required=True)
    commands.add_parser("shadow-status", help="Evaluate the advisory-hook observation gate")
    receipt = commands.add_parser("receipt-review", help="Label one shadow receipt for precision review")
    receipt.add_argument("receipt_id")
    receipt.add_argument("--label", choices=["correct", "false_positive", "unclear"], required=True)


def handle_workflow(args, service, parser, effective_db) -> int:
    del service, parser, effective_db
    store = WorkflowStore(args.workflow_db)
    try:
        result = _dispatch(args, store)
        print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True, default=str))
        return 0
    finally:
        store.close()


def _dispatch(args, store: WorkflowStore) -> Any:
    command = args.workflow_command
    if command == "scan":
        return WorkflowScanner(store, workspace_root=Path(args.workspace)).scan(
            deep=args.deep, session_ids=args.session,
        )
    if command == "inspect":
        return _inspect(store, args.session_id)
    if command == "classify":
        return classify_pending(store, limit=args.limit)
    if command == "report":
        report = build_report(store, scope=args.scope, since=args.since)
        paths = write_report(report, Path(args.output) if args.output else _default_report_dir())
        return {"report": report, "paths": {key: str(value) for key, value in paths.items()}}
    if command == "candidates":
        return {"refresh": refresh_candidates(store), "candidates": _candidate_rows(store, args.status)}
    if command == "review":
        return review_candidate(store, args.candidate_id, args.decision, rationale=args.rationale)
    if command == "proposal":
        return {"path": str(write_proposal(store, args.candidate_id, args.output)), "inert": True}
    if command == "shadow-status":
        return shadow_status(store)
    if command == "receipt-review":
        return _review_receipt(store, args.receipt_id, args.label)
    raise ValueError("unsupported workflow command")


def _inspect(store: WorkflowStore, identifier: str) -> dict[str, Any]:
    row = store.connection.execute(
        "SELECT * FROM sessions WHERE session_id=? OR external_id=?", (identifier, identifier)
    ).fetchone()
    if row is None:
        raise ValueError("workflow session does not exist")
    session_id = row["session_id"]
    session = {key: row[key] for key in row.keys() if key != "source_file_id"}
    session["worktree"] = public_excerpt(session.get("worktree"))
    return {
        "session": session,
        "turns": [dict(item) for item in store.connection.execute(
            "SELECT * FROM turns WHERE session_id=? ORDER BY ordinal", (session_id,)
        )],
        "actions": [dict(item) for item in store.connection.execute(
            "SELECT * FROM actions WHERE session_id=? ORDER BY ordinal", (session_id,)
        )],
        "feedback": [dict(item) for item in store.connection.execute(
            "SELECT * FROM feedback WHERE session_id=?", (session_id,)
        )],
    }


def _candidate_rows(store: WorkflowStore, status: str | None) -> list[dict[str, Any]]:
    if status:
        rows = store.connection.execute(
            "SELECT * FROM candidates WHERE status=? ORDER BY support_count DESC", (status,)
        )
    else:
        rows = store.connection.execute("SELECT * FROM candidates ORDER BY support_count DESC")
    return [dict(row) for row in rows]


def _review_receipt(store: WorkflowStore, receipt_id: str, label: str) -> dict[str, str]:
    changed = store.connection.execute(
        "UPDATE completion_receipts SET review_label=?,reviewed_at=? WHERE receipt_id=?",
        (label, utc_now(), receipt_id),
    ).rowcount
    store.connection.commit()
    if changed != 1:
        raise ValueError("completion receipt does not exist")
    return {"receipt_id": receipt_id, "label": label}


def shadow_status(store: WorkflowStore) -> dict[str, Any]:
    rows = store.connection.execute(
        "SELECT * FROM completion_receipts WHERE mode='shadow' ORDER BY observed_at"
    ).fetchall()
    providers: dict[str, int] = {}
    for row in rows:
        providers[row["provider"]] = providers.get(row["provider"], 0) + 1
    warning_rows = [row for row in rows if json.loads(row["warning_codes_json"] or "[]")]
    reviewed = [row for row in warning_rows if row["review_label"] in {"correct", "false_positive"}]
    correct = sum(row["review_label"] == "correct" for row in reviewed)
    precision = correct / len(reviewed) if reviewed else None
    false_read_only = sum(
        not row["mutation_seen"] and bool(json.loads(row["warning_codes_json"] or "[]")) for row in rows
    )
    requirements = _requirements(rows, providers, warning_rows, reviewed, precision, false_read_only)
    return {
        "mode": "shadow", "receipts": len(rows), "providers": providers,
        "span_days": _span_days(rows), "warnings": len(warning_rows),
        "reviewed_warnings": len(reviewed), "precision": precision,
        "read_only_false_positives": false_read_only, "requirements": requirements,
        "ready_for_operator_approval": all(requirements.values()), "activation": "never automatic",
    }


def _requirements(rows, providers, warnings, reviewed, precision, false_read_only) -> dict[str, bool]:
    return {
        "fourteen_days": _span_days(rows) >= 14,
        "one_hundred_turns": len(rows) >= 100,
        "twenty_claude": providers.get("claude", 0) >= 20,
        "twenty_codex": providers.get("codex", 0) >= 20,
        "manual_review": len(reviewed) >= min(20, len(warnings)) and bool(warnings),
        "precision_90": precision is not None and precision >= 0.9,
        "zero_read_only_false_positives": false_read_only == 0,
    }


def _span_days(rows) -> int:
    if len(rows) < 2:
        return 0
    try:
        first = datetime.fromisoformat(rows[0]["observed_at"].replace("Z", "+00:00"))
        last = datetime.fromisoformat(rows[-1]["observed_at"].replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(0, (last - first).days)


def _default_report_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path.home() / ".memorymaster" / "reports" / "workflow-intelligence" / stamp


WORKFLOW_COMMAND_HANDLERS = {"workflow": handle_workflow}

__all__ = ["WORKFLOW_COMMAND_HANDLERS", "handle_workflow", "register_workflow_parser", "shadow_status"]
