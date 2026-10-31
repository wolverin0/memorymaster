"""CLI for the recall self-check (T-1875): ``recall-selfcheck run|report``. Opt-in, never scheduled."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memorymaster.recall import selfcheck

SELFCHECK_COMMANDS = ("recall-selfcheck",)


def register_selfcheck_parser(sub: Any) -> None:
    cmd = sub.add_parser(
        "recall-selfcheck",
        help="Rehearse recall: LLM-generated questions from recent confirmed claims, hit@k in a sidecar (read-only on the DB)",
    )
    actions = cmd.add_subparsers(dest="selfcheck_action", required=True)
    run = actions.add_parser("run", help="Generate questions (calls the configured LLM provider) and check recall")
    run.add_argument("--sidecar", default=None, help="Result file (default: <db>.recall-selfcheck.sqlite)")
    run.add_argument("--k", type=int, default=selfcheck.DEFAULT_K, help="A claim is a hit if it ranks in the top k")
    run.add_argument("--max-claims", type=int, default=selfcheck.DEFAULT_MAX_CLAIMS, help="Claims per run (also the LLM call cap)")
    run.add_argument("--questions", type=int, default=selfcheck.DEFAULT_QUESTIONS, help="Questions per claim (1-5)")
    run.add_argument("--since-days", type=int, default=selfcheck.DEFAULT_SINCE_DAYS, help="Only claims created this recently")
    run.add_argument("--max-tokens", type=int, default=selfcheck.DEFAULT_MAX_TOKENS, help="Estimated token cap per run")
    run.add_argument("--scope", default=None, help="Only this claim scope")
    run.add_argument("--retrieval-mode", choices=["legacy", "hybrid"], default="legacy")
    report = actions.add_parser("report", help="Hit rate per scope and the claims recall could not find")
    report.add_argument("--sidecar", default=None, help="Result file (default: <db>.recall-selfcheck.sqlite)")
    report.add_argument("--scope", default=None)
    report.add_argument("--limit", type=int, default=50, help="Max unrecoverable claims listed")


def handle_selfcheck(args: Any, service: Any, parser: Any, effective_db: str) -> int:
    del service, parser
    sidecar = Path(args.sidecar) if args.sidecar else selfcheck.default_sidecar_path(effective_db)
    if args.selfcheck_action == "run":
        run = selfcheck.run_selfcheck(
            effective_db, sidecar_path=sidecar, k=args.k, max_claims=args.max_claims,
            questions_per_claim=args.questions, since_days=args.since_days,
            max_tokens=args.max_tokens, scope=args.scope, retrieval_mode=args.retrieval_mode,
        )
        print(json.dumps(run.to_dict(), ensure_ascii=True, indent=2))
        return 0 if run.status in ("ok", "no_candidates") else 3
    print(json.dumps(selfcheck.build_report(sidecar, scope=args.scope, limit=args.limit),
                     ensure_ascii=True, indent=2))
    return 0


SELFCHECK_COMMAND_HANDLERS = {"recall-selfcheck": handle_selfcheck}
