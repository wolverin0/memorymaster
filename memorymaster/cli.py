from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path

from memorymaster.models import CLAIM_STATUSES, CitationInput, VOLATILITY_LEVELS
from memorymaster.policy import POLICY_MODES
from memorymaster.retrieval import RETRIEVAL_MODES
from memorymaster.scheduler import run_daemon
from memorymaster.security import resolve_allow_sensitive_access
from memorymaster.service import MemoryService


def parse_citation(raw: str) -> CitationInput:
    # Format: source|locator|excerpt (locator/excerpt optional).
    parts = [part.strip() for part in raw.split("|", 2)]
    source = parts[0] if parts else ""
    if not source:
        raise ValueError("Citation source is required.")
    locator = parts[1] if len(parts) > 1 and parts[1] else None
    excerpt = parts[2] if len(parts) > 2 and parts[2] else None
    return CitationInput(source=source, locator=locator, excerpt=excerpt)


def parse_scope_allowlist(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    values = [part.strip() for part in raw.split(",") if part.strip()]
    return values or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memorymaster", description="Memory reliability MVP CLI")
    parser.add_argument(
        "--db",
        default="memorymaster.db",
        help="SQLite path or Postgres DSN (postgresql://...)",
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Workspace root used for deterministic codebase checks and git-triggered scheduling",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create schema in SQLite database")

    ingest = sub.add_parser("ingest", help="Ingest a raw claim with citations")
    ingest.add_argument("--text", required=True, help="Claim text")
    ingest.add_argument(
        "--source",
        action="append",
        default=[],
        help="Citation in format source|locator|excerpt (repeat for multiple citations)",
    )
    ingest.add_argument("--claim-type", help="Optional claim type label")
    ingest.add_argument("--subject", help="Optional claim subject")
    ingest.add_argument("--predicate", help="Optional claim predicate")
    ingest.add_argument("--object", dest="object_value", help="Optional claim object/value")
    ingest.add_argument("--idempotency-key", help="Optional key to dedupe ingest retries")
    ingest.add_argument("--scope", default="project", help="Claim scope (default: project)")
    ingest.add_argument("--volatility", choices=list(VOLATILITY_LEVELS), default="medium")
    ingest.add_argument("--confidence", type=float, default=0.5, help="Initial confidence (0-1)")

    cycle = sub.add_parser("run-cycle", help="Run extractor, validator, decay, and optional compact")
    cycle.add_argument("--with-compact", action="store_true", help="Run compactor at the end of cycle")
    cycle.add_argument("--min-citations", type=int, default=1, help="Minimum citations to confirm candidate")
    cycle.add_argument("--min-score", type=float, default=0.58, help="Minimum score to confirm candidate")
    cycle.add_argument(
        "--policy-mode",
        choices=list(POLICY_MODES),
        default="legacy",
        help="Revalidation policy mode (legacy keeps candidate-only validation)",
    )
    cycle.add_argument("--policy-limit", type=int, default=200, help="Max due claims selected for revalidation")

    query = sub.add_parser("query", help="Search claims by text")
    query.add_argument("text", help="Query text")
    query.add_argument("--limit", type=int, default=20, help="Maximum rows")
    query.add_argument("--exclude-stale", action="store_true", help="Only return confirmed/conflicted")
    query.add_argument("--exclude-conflicted", action="store_true", help="Only return confirmed/stale")
    query.add_argument("--include-candidates", action="store_true", help="Also search candidate (unverified) claims")
    query.add_argument(
        "--retrieval-mode",
        choices=list(RETRIEVAL_MODES),
        default="legacy",
        help="Retrieval mode (legacy SQL ordering or hybrid lexical/confidence/freshness ranking)",
    )
    query.add_argument(
        "--allow-sensitive",
        action="store_true",
        help="Include claims that look sensitive (default excludes them)",
    )
    query.add_argument(
        "--scope-allowlist",
        default="",
        help="Comma-separated scopes to include (e.g. project,team_x)",
    )

    pin = sub.add_parser("pin", help="Pin or unpin a claim")
    pin.add_argument("claim_id", type=int, help="Claim id")
    pin.add_argument("--unpin", action="store_true", help="Unpin instead of pinning")

    redact_claim = sub.add_parser("redact-claim", help="Non-destructive redact/erase workflow for claim payload")
    redact_claim.add_argument("claim_id", type=int, help="Claim id")
    redact_claim.add_argument("--mode", choices=["redact", "erase"], default="redact", help="Workflow mode")
    redact_target = redact_claim.add_mutually_exclusive_group()
    redact_target.add_argument("--claims-only", action="store_true", help="Only scrub claim fields")
    redact_target.add_argument("--citations-only", action="store_true", help="Only scrub citation fields")
    redact_claim.add_argument("--reason", default="", help="Optional audit reason")
    redact_claim.add_argument("--actor", default="cli", help="Audit source value")

    compact = sub.add_parser("compact", help="Archive stale/superseded/conflicted claims and trim old events")
    compact.add_argument(
        "--retain-days",
        type=int,
        default=30,
        help="Days before archiving stale/superseded/conflicted claims",
    )
    compact.add_argument("--event-retain-days", type=int, default=60, help="Days to retain event history")

    list_claims = sub.add_parser("list-claims", help="List claims")
    list_claims.add_argument("--status", choices=list(CLAIM_STATUSES), help="Filter by claim status")
    list_claims.add_argument("--limit", type=int, default=50, help="Maximum rows")
    list_claims.add_argument("--include-archived", action="store_true", help="Include archived claims")
    list_claims.add_argument(
        "--allow-sensitive",
        action="store_true",
        help="Include claims that look sensitive (default excludes them)",
    )

    list_events = sub.add_parser("list-events", help="List events")
    list_events.add_argument("--claim-id", type=int, help="Filter by claim id")
    list_events.add_argument("--event-type", help="Filter by event type")
    list_events.add_argument("--limit", type=int, default=100, help="Maximum rows")

    export_metrics = sub.add_parser("export-metrics", help="Export D3 structured metrics from JSONL events")
    export_metrics.add_argument(
        "--events-jsonl",
        action="append",
        required=True,
        help="Path to JSONL events input (repeat flag for multiple files)",
    )
    export_metrics.add_argument(
        "--out-prom",
        default="artifacts/metrics/metrics.prom",
        help="Output path for Prometheus text metrics",
    )
    export_metrics.add_argument(
        "--out-json",
        default="artifacts/metrics/metrics_snapshot.json",
        help="Output path for structured metrics JSON snapshot",
    )

    review_queue = sub.add_parser("review-queue", help="Build conflict/stale review queue")
    review_queue.add_argument("--limit", type=int, default=100, help="Maximum claims scanned for queue")
    review_queue.add_argument("--exclude-stale", action="store_true", help="Exclude stale claims from queue")
    review_queue.add_argument("--exclude-conflicted", action="store_true", help="Exclude conflicted claims from queue")
    review_queue.add_argument(
        "--allow-sensitive",
        action="store_true",
        help="Include claims that look sensitive (default excludes them)",
    )

    daemon = sub.add_parser("run-daemon", help="Run scheduler loop for periodic/background memory maintenance")
    daemon.add_argument("--interval-seconds", type=int, default=3600, help="Timer-based cycle interval")
    daemon.add_argument("--max-cycles", type=int, help="Exit after N cycles")
    daemon.add_argument("--compact-every", type=int, default=0, help="Run compactor every N cycles (0 disables)")
    daemon.add_argument("--min-citations", type=int, default=1, help="Minimum citations to confirm candidate")
    daemon.add_argument("--min-score", type=float, default=0.58, help="Minimum score to confirm candidate")
    daemon.add_argument(
        "--policy-mode",
        choices=list(POLICY_MODES),
        default="legacy",
        help="Revalidation policy mode (legacy keeps candidate-only validation)",
    )
    daemon.add_argument("--policy-limit", type=int, default=200, help="Max due claims selected for revalidation")
    daemon.add_argument("--git-trigger", action="store_true", help="Run cycle when git HEAD changes")
    daemon.add_argument("--git-check-seconds", type=int, default=10, help="How often to poll git HEAD")

    dashboard = sub.add_parser("run-dashboard", help="Run read-only HTTP dashboard/API")
    dashboard.add_argument("--host", default="127.0.0.1", help="Bind host")
    dashboard.add_argument("--port", type=int, default=8765, help="Bind port")
    dashboard.add_argument(
        "--operator-log-jsonl",
        default="artifacts/operator/operator_events.jsonl",
        help="Path consumed by /api/operator/stream",
    )

    operator = sub.add_parser("run-operator", help="Run pre/post-turn memory maintenance loop from JSONL inbox")
    operator.add_argument("--inbox-jsonl", required=True, help="Path to JSONL turn-event inbox")
    operator.add_argument("--poll-seconds", type=float, default=1.0, help="Polling interval for inbox tailing")
    operator.add_argument("--max-events", type=int, help="Exit after processing N turn events")
    operator.add_argument(
        "--max-idle-seconds",
        type=float,
        default=0.0,
        help="Exit if no new inbox lines arrive for N seconds (0 disables)",
    )
    operator.add_argument(
        "--reconcile-seconds",
        type=float,
        default=300,
        help="Periodic reconciliation interval for background maintenance",
    )
    operator.add_argument(
        "--retrieval-mode",
        choices=list(RETRIEVAL_MODES),
        default="hybrid",
        help="Retrieval mode for pre-turn memory query",
    )
    operator.add_argument("--query-limit", type=int, default=8, help="Max claims to fetch during pre-turn retrieval")
    operator.add_argument(
        "--disable-progressive-retrieval",
        action="store_true",
        help="Use a single retrieval query instead of progressive tiered retrieval",
    )
    operator.add_argument("--tier1-limit", type=int, default=4, help="Tier-1 retrieval limit when progressive retrieval is enabled")
    operator.add_argument("--tier2-limit", type=int, default=8, help="Tier-2 retrieval limit when progressive retrieval falls back")
    operator.add_argument("--min-citations", type=int, default=1, help="Minimum citations to confirm candidate")
    operator.add_argument("--min-score", type=float, default=0.58, help="Minimum score to confirm candidate")
    operator.add_argument(
        "--policy-mode",
        choices=list(POLICY_MODES),
        default="cadence",
        help="Revalidation policy mode for post-turn maintenance",
    )
    operator.add_argument("--policy-limit", type=int, default=200, help="Max due claims selected for revalidation")
    operator.add_argument("--compact-every", type=int, default=0, help="Run compactor every N processed turns")
    operator.add_argument(
        "--log-jsonl",
        default="artifacts/operator/operator_events.jsonl",
        help="JSONL path for operator run events (empty disables logging)",
    )
    operator.add_argument(
        "--state-json",
        default="artifacts/operator/operator_state.json",
        help="JSON path for operator checkpoint state (empty disables state persistence)",
    )
    operator.add_argument(
        "--queue-state-json",
        default="artifacts/operator/operator_queue_state.json",
        help="JSON path for durable pending queue state (empty disables durable queue state persistence)",
    )
    operator.add_argument(
        "--queue-journal-jsonl",
        default="artifacts/operator/operator_queue_journal.jsonl",
        help="JSONL append-only journal path for durable queue enqueue/ack events (empty disables queue journal)",
    )
    operator.add_argument(
        "--no-state",
        action="store_true",
        help="Disable checkpoint and durable queue state load/save",
    )

    steward = sub.add_parser("run-steward", help="Run claim stewardship probes and proposal generation")
    steward.add_argument("--mode", choices=["manual", "cadence"], default="manual", help="Loop mode")
    steward.add_argument(
        "--cadence-trigger",
        choices=["timer", "commit", "timer_or_commit"],
        default="timer",
        help="Cadence trigger strategy when mode=cadence",
    )
    steward.add_argument("--interval-seconds", type=float, default=30.0, help="Sleep interval between cadence cycles")
    steward.add_argument(
        "--git-check-seconds",
        type=float,
        default=10.0,
        help="Git polling interval for commit-triggered cadence",
    )
    steward.add_argument(
        "--commit-every",
        type=int,
        default=1,
        help="Run a stewardship cycle after N observed git head changes",
    )
    steward.add_argument("--max-cycles", type=int, default=1, help="Number of cycles to run")
    steward.add_argument("--max-claims", type=int, default=200, help="Max claims scanned per cycle")
    steward.add_argument("--max-proposals", type=int, default=200, help="Max proposal events emitted per cycle")
    steward.add_argument("--max-probe-files", type=int, default=200, help="Max files scanned for filesystem probe")
    steward.add_argument(
        "--max-probe-file-bytes",
        type=int,
        default=524288,
        help="Skip files larger than this byte size during filesystem probe",
    )
    steward.add_argument(
        "--max-tool-probes",
        type=int,
        default=200,
        help="Maximum tool probe executions per cycle",
    )
    steward.add_argument(
        "--probe-timeout-seconds",
        type=float,
        default=2.0,
        help="Per-probe timeout budget in seconds",
    )
    steward.add_argument(
        "--probe-failure-threshold",
        type=int,
        default=3,
        help="Open circuit breaker for a probe type after this many timeout/error failures",
    )
    steward.add_argument(
        "--disable-semantic-probe",
        action="store_true",
        help="Disable semantic retrieval probe in steward planner",
    )
    steward.add_argument(
        "--disable-tool-probe",
        action="store_true",
        help="Disable tool/storage probe in steward planner",
    )
    steward.add_argument("--allow-sensitive", action="store_true", help="Include sensitive claims in stewardship scan")
    steward.add_argument("--apply", action="store_true", help="Apply proposed status transitions")
    steward.add_argument(
        "--artifact-json",
        default="artifacts/steward/steward_report.json",
        help="Path to steward JSON report artifact",
    )

    steward_proposals = sub.add_parser("steward-proposals", help="List steward proposal events for human override")
    steward_proposals.add_argument("--limit", type=int, default=100, help="Maximum proposals returned")
    steward_proposals.add_argument(
        "--include-resolved",
        action="store_true",
        help="Include already approved/rejected proposals",
    )

    resolve_proposal = sub.add_parser("resolve-proposal", help="Approve or reject steward proposal")
    resolve_proposal.add_argument(
        "--action",
        choices=["approve", "reject"],
        required=True,
        help="Resolution action",
    )
    resolve_proposal.add_argument("--proposal-event-id", type=int, help="Specific steward proposal event id")
    resolve_proposal.add_argument("--claim-id", type=int, help="Resolve latest pending proposal for claim id")
    resolve_proposal.add_argument(
        "--no-apply",
        action="store_true",
        help="When approving, do not apply state transition; only mark proposal approved",
    )

    return parser


def print_claim(claim) -> None:
    line = (
        f"[{claim.id}] {claim.status:<10} conf={claim.confidence:.3f} pin={int(claim.pinned)} "
        f"type={claim.claim_type or '-'} tuple=({claim.subject or '-'}, {claim.predicate or '-'}, {claim.object_value or '-'}) "
        f"scope={claim.scope} vol={claim.volatility} updated={claim.updated_at}"
    )
    print(line)
    print(f"  text: {claim.text}")
    if claim.supersedes_claim_id or claim.replaced_by_claim_id:
        print(
            f"  links: supersedes={claim.supersedes_claim_id or '-'} replaced_by={claim.replaced_by_claim_id or '-'}"
        )
    for citation in claim.citations:
        locator = f" | {citation.locator}" if citation.locator else ""
        excerpt = f" | {citation.excerpt}" if citation.excerpt else ""
        print(f"  - cite: {citation.source}{locator}{excerpt}")


def _json_default(value):
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    if hasattr(value, "__dict__"):
        return value.__dict__
    return repr(value)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "export-metrics":
            from memorymaster.metrics_exporter import export_metrics

            snapshot = export_metrics(
                events_jsonl=[Path(path) for path in args.events_jsonl],
                out_prom=Path(args.out_prom),
                out_json=Path(args.out_json),
            )
            payload = {
                "command": "export-metrics",
                "events_total": int(snapshot.get("counters", {}).get("events_total", 0)),
                "transitions_total": int(snapshot.get("counters", {}).get("transitions_total", 0)),
                "status_total": int(snapshot.get("counters", {}).get("status_total", 0)),
                "out_prom": str(Path(args.out_prom)),
                "out_json": str(Path(args.out_json)),
            }
            print(json.dumps(payload, indent=2))
            return 0

        service = MemoryService(args.db, workspace_root=Path(args.workspace))

        if args.command == "init-db":
            service.init_db()
            if "://" in str(args.db):
                print(f"initialized db: {args.db}")
            else:
                print(f"initialized db: {Path(args.db).resolve()}")
            return 0

        if args.command == "ingest":
            citations = [parse_citation(raw) for raw in args.source]
            claim = service.ingest(
                text=args.text,
                citations=citations,
                idempotency_key=args.idempotency_key,
                claim_type=args.claim_type,
                subject=args.subject,
                predicate=args.predicate,
                object_value=args.object_value,
                scope=args.scope,
                volatility=args.volatility,
                confidence=args.confidence,
            )
            print(f"ingested claim_id={claim.id} status={claim.status} citations={len(claim.citations)}")
            return 0

        if args.command == "run-cycle":
            result = service.run_cycle(
                run_compactor=args.with_compact,
                min_citations=args.min_citations,
                min_score=args.min_score,
                policy_mode=args.policy_mode,
                policy_limit=args.policy_limit,
            )
            print(json.dumps(result, indent=2))
            return 0

        if args.command == "query":
            resolve_allow_sensitive_access(
                allow_sensitive=args.allow_sensitive,
                context="cli.query",
            )
            rows_data = service.query_rows(
                query_text=args.text,
                limit=args.limit,
                include_stale=not args.exclude_stale,
                include_conflicted=not args.exclude_conflicted,
                include_candidates=getattr(args, "include_candidates", False),
                retrieval_mode=args.retrieval_mode,
                allow_sensitive=args.allow_sensitive,
                scope_allowlist=parse_scope_allowlist(args.scope_allowlist),
            )
            for row in rows_data:
                claim = row["claim"]
                print_claim(claim)
                annotation = row.get("annotation", {})
                print(
                    "  retrieval: "
                    f"score={float(row.get('score', 0.0)):.3f} "
                    f"lex={float(row.get('lexical_score', 0.0)):.3f} "
                    f"conf={float(row.get('confidence_score', 0.0)):.3f} "
                    f"fresh={float(row.get('freshness_score', 0.0)):.3f} "
                    f"vec={float(row.get('vector_score', 0.0)):.3f} "
                    f"active={int(bool(annotation.get('active', False)))} "
                    f"stale={int(bool(annotation.get('stale', False)))} "
                    f"conflicted={int(bool(annotation.get('conflicted', False)))} "
                    f"pinned={int(bool(annotation.get('pinned', False)))}"
                )
            print(f"rows={len(rows_data)}")
            return 0

        if args.command == "pin":
            claim = service.pin(args.claim_id, pin=not args.unpin)
            print(f"claim_id={claim.id} status={claim.status} pinned={int(claim.pinned)}")
            return 0

        if args.command == "redact-claim":
            result = service.redact_claim_payload(
                args.claim_id,
                mode=args.mode,
                redact_claim=not args.citations_only,
                redact_citations=not args.claims_only,
                reason=(args.reason.strip() or None),
                actor=args.actor,
            )
            print(json.dumps(result, indent=2, default=_json_default))
            return 0

        if args.command == "compact":
            result = service.compact(retain_days=args.retain_days, event_retain_days=args.event_retain_days)
            print(json.dumps(result, indent=2))
            return 0

        if args.command == "list-claims":
            resolve_allow_sensitive_access(
                allow_sensitive=args.allow_sensitive,
                context="cli.list-claims",
            )
            claims = service.list_claims(
                status=args.status,
                limit=args.limit,
                include_archived=args.include_archived,
                allow_sensitive=args.allow_sensitive,
            )
            for claim in claims:
                print_claim(claim)
            print(f"rows={len(claims)}")
            return 0

        if args.command == "list-events":
            events = service.list_events(
                claim_id=args.claim_id,
                limit=args.limit,
                event_type=args.event_type,
            )
            print(json.dumps({"rows": len(events), "events": events}, indent=2, default=_json_default))
            return 0

        if args.command == "review-queue":
            from memorymaster.review import build_review_queue, queue_to_dicts

            include_sensitive = resolve_allow_sensitive_access(
                allow_sensitive=args.allow_sensitive,
                context="cli.review-queue",
            )
            items = build_review_queue(
                service,
                limit=args.limit,
                include_stale=not args.exclude_stale,
                include_conflicted=not args.exclude_conflicted,
                include_sensitive=include_sensitive,
            )
            payload = {
                "rows": len(items),
                "items": queue_to_dicts(items),
            }
            print(json.dumps(payload, indent=2))
            return 0

        if args.command == "run-daemon":
            result = run_daemon(
                service,
                interval_seconds=args.interval_seconds,
                max_cycles=args.max_cycles,
                compact_every=args.compact_every,
                min_citations=args.min_citations,
                min_score=args.min_score,
                policy_mode=args.policy_mode,
                policy_limit=args.policy_limit,
                git_trigger=args.git_trigger,
                git_check_seconds=args.git_check_seconds,
            )
            print(json.dumps(result, indent=2))
            return 0

        if args.command == "run-dashboard":
            from memorymaster.dashboard import create_dashboard_server

            server = create_dashboard_server(
                db_target=args.db,
                workspace_root=args.workspace,
                host=args.host,
                port=args.port,
                operator_log_jsonl=args.operator_log_jsonl,
            )
            print(f"memorymaster dashboard listening on http://{args.host}:{args.port}/dashboard")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0

        if args.command == "run-operator":
            try:
                from memorymaster.operator import MemoryOperator, OperatorConfig
            except Exception as exc:
                print(
                    "error: run-operator unavailable: could not import memorymaster.operator "
                    f"({exc})"
                )
                return 2

            config = OperatorConfig(
                reconcile_interval_seconds=args.reconcile_seconds,
                retrieval_mode=args.retrieval_mode,
                retrieval_limit=args.query_limit,
                progressive_retrieval=not args.disable_progressive_retrieval,
                tier1_limit=args.tier1_limit,
                tier2_limit=args.tier2_limit,
                min_citations=args.min_citations,
                min_score=args.min_score,
                policy_mode=args.policy_mode,
                policy_limit=args.policy_limit,
                compact_every=args.compact_every,
                max_idle_seconds=(args.max_idle_seconds if args.max_idle_seconds and args.max_idle_seconds > 0 else None),
                log_jsonl_path=(args.log_jsonl.strip() if str(args.log_jsonl).strip() else None),
                state_json_path=(
                    None
                    if args.no_state
                    else (args.state_json.strip() if str(args.state_json).strip() else None)
                ),
                queue_state_json_path=(
                    None
                    if args.no_state
                    else (args.queue_state_json.strip() if str(args.queue_state_json).strip() else None)
                ),
                queue_journal_jsonl_path=(
                    None
                    if args.no_state
                    else (args.queue_journal_jsonl.strip() if str(args.queue_journal_jsonl).strip() else None)
                ),
            )

            operator = MemoryOperator(service=service, config=config)
            result = operator.run_stream(
                inbox_jsonl=Path(args.inbox_jsonl),
                poll_seconds=args.poll_seconds,
                max_events=args.max_events,
            )

            print(json.dumps(result, indent=2, default=_json_default))
            return 0

        if args.command == "run-steward":
            from memorymaster.steward import run_steward

            allow_sensitive = resolve_allow_sensitive_access(
                allow_sensitive=args.allow_sensitive,
                context="cli.run-steward",
            )
            result = run_steward(
                service,
                mode=args.mode,
                cadence_trigger=args.cadence_trigger,
                interval_seconds=args.interval_seconds,
                git_check_seconds=args.git_check_seconds,
                commit_every=args.commit_every,
                max_cycles=args.max_cycles,
                allow_sensitive=allow_sensitive,
                apply=args.apply,
                max_claims=args.max_claims,
                max_proposals=args.max_proposals,
                max_probe_files=args.max_probe_files,
                max_probe_file_bytes=args.max_probe_file_bytes,
                max_tool_probes=args.max_tool_probes,
                probe_timeout_seconds=args.probe_timeout_seconds,
                probe_failure_threshold=args.probe_failure_threshold,
                enable_semantic_probe=not args.disable_semantic_probe,
                enable_tool_probe=not args.disable_tool_probe,
                artifact_path=Path(args.artifact_json),
            )
            print(json.dumps(result, indent=2, default=_json_default))
            return 0

        if args.command == "steward-proposals":
            from memorymaster.steward import list_steward_proposals

            rows = list_steward_proposals(
                service,
                limit=args.limit,
                include_resolved=args.include_resolved,
            )
            print(json.dumps({"rows": len(rows), "proposals": rows}, indent=2, default=_json_default))
            return 0

        if args.command == "resolve-proposal":
            if args.proposal_event_id is None and args.claim_id is None:
                raise ValueError("resolve-proposal requires --proposal-event-id or --claim-id")
            from memorymaster.steward import resolve_steward_proposal

            result = resolve_steward_proposal(
                service,
                action=args.action,
                proposal_event_id=args.proposal_event_id,
                claim_id=args.claim_id,
                apply_on_approve=not args.no_apply,
            )
            print(json.dumps(result, indent=2, default=_json_default))
            return 0

        parser.print_help()
        return 1
    except Exception as exc:
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
