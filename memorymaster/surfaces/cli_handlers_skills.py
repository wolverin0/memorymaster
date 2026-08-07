"""CLI wiring for governed skill proposal, review, recall, and staging.

All mutating commands operate on candidate claims and explicit human review;
none writes to Claude, Codex, or Hermes skill directories. JSON proposal input
is validated by the shared personal-skill-v1 boundary before persistence.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from memorymaster.knowledge.skills import (
    approve_skill_candidate,
    collect_skill_proposal_inputs,
    export_confirmed_skills,
    propose_skill,
    recall_skills,
    reject_skill_candidate,
)


def register_skill_parsers(sub: Any) -> None:
    inputs = sub.add_parser("skill-inputs", help="List recurring rule evidence eligible for skill review")
    inputs.add_argument("--scope", required=True, help="Exact governed scope")
    inputs.add_argument("--min-corrections", type=int, default=2)
    inputs.add_argument("--limit", type=int, default=20)

    propose = sub.add_parser("skill-propose", help="Create one governed skill candidate from JSON")
    propose.add_argument("--input", required=True, help="JSON file, or - for stdin")
    propose.add_argument("--scope", required=True, help="Exact governed scope")
    propose.add_argument("--supporting-claim-id", action="append", type=int, required=True)
    propose.add_argument("--source-agent", default="skill-reviewer-cli")

    review = sub.add_parser("skill-review", help="Explicitly approve or reject a skill candidate")
    review.add_argument("--claim-id", type=int, required=True)
    review.add_argument("--action", choices=("approve", "reject"), required=True)
    review.add_argument("--actor", default="operator-cli")
    review.add_argument("--reason", default="")

    recall = sub.add_parser("skill-recall", help="Recall confirmed governed skills only")
    recall.add_argument("query")
    recall.add_argument("--scope", action="append", required=True, dest="scopes")
    recall.add_argument("--limit", type=int, default=10)

    export = sub.add_parser("skill-export", help="Render confirmed skills under MemoryMaster staging")
    export.add_argument("--output", default="", help="Staging root (default: ~/.memorymaster/staging/skills)")
    export.add_argument("--scope", action="append", dest="scopes")
    export.add_argument("--limit", type=int, default=200)


def _emit(args: Any, result: dict[str, Any]) -> int:
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _read_payload(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("skill proposal input must be a JSON object")
    return payload


def handle_skill_inputs(args: Any, service: Any, _parser: Any, _db: str) -> int:
    rows = collect_skill_proposal_inputs(
        service,
        scope=args.scope,
        min_corrections=args.min_corrections,
        limit=args.limit,
    )
    return _emit(args, {"ok": True, "rows": len(rows), "inputs": rows})


def handle_skill_propose(args: Any, service: Any, _parser: Any, _db: str) -> int:
    result = propose_skill(
        service,
        payload=_read_payload(args.input),
        supporting_claim_ids=args.supporting_claim_id,
        scope=args.scope,
        source_agent=args.source_agent,
    )
    return _emit(args, result)


def handle_skill_review(args: Any, service: Any, _parser: Any, _db: str) -> int:
    if args.action == "approve":
        result = approve_skill_candidate(service, args.claim_id, actor=args.actor)
    else:
        result = reject_skill_candidate(
            service,
            args.claim_id,
            actor=args.actor,
            reason=args.reason or "operator rejected candidate",
        )
    return _emit(args, result)


def handle_skill_recall(args: Any, service: Any, _parser: Any, _db: str) -> int:
    rows = recall_skills(
        service,
        args.query,
        scope_allowlist=args.scopes,
        limit=args.limit,
    )
    return _emit(args, {"ok": True, "rows": len(rows), "skills": rows})


def handle_skill_export(args: Any, service: Any, _parser: Any, _db: str) -> int:
    result = export_confirmed_skills(
        service,
        staging_root=args.output or None,
        scope_allowlist=args.scopes,
        limit=args.limit,
    )
    return _emit(args, result)


SKILL_COMMAND_HANDLERS = {
    "skill-inputs": handle_skill_inputs,
    "skill-propose": handle_skill_propose,
    "skill-review": handle_skill_review,
    "skill-recall": handle_skill_recall,
    "skill-export": handle_skill_export,
}
