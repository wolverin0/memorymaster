"""Explicit operator CLI; no service construction for inventory/status/dry-run."""
from __future__ import annotations

import json
from pathlib import Path

from memorymaster.dreaming.backfill import BackfillConfig, BackfillRunner, read_backfill_status, rollback_backfill_cohort


def register_backfill_parser(sub) -> None:
    command = sub.add_parser("dream-backfill", help="Explicit historical sampling campaign (never scheduled)")
    actions = command.add_subparsers(dest="backfill_action", required=True)
    for name in ("inventory", "run", "status", "pause", "resume", "rollback"):
        action = actions.add_parser(name)
        action.add_argument("--dry-run", action="store_true", help="Read fixtures/state and print only; no writes or providers")
        action.add_argument("--state-dir", type=Path, required=True, help="Explicit capture-state directory for isolated backfill artifacts")
        action.add_argument("--campaign", default="ao7")
        action.add_argument("--cohort", default=None)
        action.add_argument("--source-root", action="append", default=[], metavar="PROVIDER=PATH")
        action.add_argument("--offline-metadata", type=Path, default=None)
        if name == "run":
            action.add_argument("--client", choices=("opencode", "gemini-api"), default="opencode")
            action.add_argument("--stage", choices=("extract", "consolidate", "quality", "apply", "all"), default="extract")
            action.add_argument("--apply-candidates", action="store_true", help="Explicitly authorize gated candidate/proposal writes")
            action.add_argument("--extractor-model", default="")
            action.add_argument("--consolidator-model", default="")
            action.add_argument("--memory-db", type=Path, default=None, help="Explicit authoritative target, required only for reference/application stages")
            action.add_argument("--haiku-model", default="", help="Exact provider-qualified quality model")
            action.add_argument("--sonnet-model", default="", help="Exact provider-qualified independent quality model")
            action.add_argument("--probe-scope", default=None, help="Authorized scope for the read-only SQLite canary")
            action.add_argument("--dense-log", type=Path, default=None)
            action.add_argument("--hook-log", type=Path, default=None)
        if name == "resume":
            action.add_argument("--evidence", type=Path, default=None, help="Diagnosis, disposition and fresh passing gate evidence JSON")
        if name == "rollback":
            action.add_argument("--apply", action="store_true", help="Authorize lifecycle retirement of exclusively created claims")
            action.add_argument("--memory-db", type=Path, default=None)


def configure_backfill_runtime(args, runner: BackfillRunner, *, client_factory=None) -> None:
    """Bind explicit existing transports. Unsupported output caps fail preflight.

    A lane may inject a verified bounded client through this interface; the
    stock OpenCode CLI currently cannot prove an enforceable output limit.
    Gemini API uses the existing key-based transport with a provider output cap.
    """
    from memorymaster.core.opencode_client import OpenCodeClient
    from memorymaster.dreaming.providers import GeminiBoundedClient, OpenCodeConsolidator, OpenCodeExtractor
    from memorymaster.dreaming.backfill_quality import ProviderCohortJudge
    from memorymaster.dreaming.backfill_guard import run_quiet_hours_active_probes
    from memorymaster.dreaming.latency_guard import GuardDecision, evaluate

    factory = client_factory or OpenCodeClient
    if getattr(args, "client", "opencode") == "gemini-api":
        factory = client_factory or GeminiBoundedClient
    if not args.extractor_model or "/" not in args.extractor_model:
        raise ValueError("run requires an explicit provider-qualified extraction model")
    slot = args.state_dir / ("provider-slot-" + args.campaign)

    def bounded(model):
        client = factory(model=model, work_dir=slot)
        if not callable(getattr(client, "complete_bounded", None)):
            raise ValueError("configured route cannot enforce provider output limits; bind a verified bounded client before running")
        return client

    runner.extractor = OpenCodeExtractor(model=args.extractor_model, work_dir=slot,
                                         max_input_tokens=10_000, max_output_tokens=1500, framing_tokens=512,
                                         client=bounded(args.extractor_model))
    runner.guard = lambda stage: (evaluate(dense_log=args.dense_log, hook_log=args.hook_log, record=False)
                                  if args.dense_log and args.hook_log else GuardDecision("allow", ["insufficient_data"]))
    if args.stage != "extract":
        if args.memory_db is None or not args.consolidator_model or not args.probe_scope:
            raise ValueError("authoritative stages require an explicit database, qualified Sonnet model and authorized probe scope")
        from memorymaster.core.service import MemoryService

        runner.consolidator = OpenCodeConsolidator(model=args.consolidator_model, work_dir=slot,
                                                  client=bounded(args.consolidator_model))
        runner.reference_service_factory = lambda: MemoryService(args.memory_db, workspace_root=args.workspace, read_only=True,
                                                                 tenant_id=getattr(args, "tenant", None))
        runner.application_service_factory = lambda: MemoryService(args.memory_db, workspace_root=args.workspace,
                                                                   tenant_id=getattr(args, "tenant", None))
        runner.active_probes = lambda: run_quiet_hours_active_probes(
            service_factory=runner.reference_service_factory, scope=args.probe_scope)
        if args.stage in {"quality", "apply", "all"}:
            for name, required in (("haiku", "haiku"), ("sonnet", "sonnet")):
                model = getattr(args, name + "_model")
                if "/" not in model or required not in model.lower():
                    raise ValueError("quality requires exact qualified Haiku and Sonnet judge models")
                setattr(runner, name, ProviderCohortJudge(bounded(model), provider=model.split("/", 1)[0], model=model))


def handle_backfill(args, service, parser, effective_db) -> int:
    roots = {}
    for raw in args.source_root:
        provider, separator, path = raw.partition("=")
        if not separator or not path:
            raise ValueError("source root must be PROVIDER=PATH")
        roots[provider] = Path(path)
    config = BackfillConfig(args.state_dir, roots, args.campaign,
                            extractor_model=getattr(args, "extractor_model", ""),
                            consolidator_model=getattr(args, "consolidator_model", ""))
    runner = BackfillRunner(config)
    metadata = json.loads(args.offline_metadata.read_text(encoding="utf-8")) if args.offline_metadata else None
    if args.backfill_action == "inventory":
        report = runner.inventory(dry_run=args.dry_run, metadata=metadata)
    elif args.backfill_action == "status":
        report = read_backfill_status(args.state_dir, campaign_id=args.campaign, dry_run=args.dry_run)
    elif args.backfill_action == "pause":
        report = runner.pause(cohort_id=args.cohort or "campaign", dry_run=args.dry_run)
    elif args.backfill_action == "resume":
        evidence = json.loads(args.evidence.read_text(encoding="utf-8")) if args.evidence else {}
        report = runner.resume(cohort_id=args.cohort or "campaign", evidence=evidence, dry_run=args.dry_run)
    elif args.backfill_action == "rollback":
        if not args.cohort:
            raise ValueError("rollback requires an exact cohort")
        dry = args.dry_run or not args.apply
        if not dry:
            if args.memory_db is None:
                raise ValueError("rollback application requires explicit memory database")
            from memorymaster.core.service import MemoryService

            service = MemoryService(args.memory_db, workspace_root=args.workspace, tenant_id=getattr(args, "tenant", None))
        report = rollback_backfill_cohort(runner, args.cohort, service=service, dry_run=dry)
    else:
        if args.dry_run:
            report = runner.inventory(dry_run=True, metadata=metadata)
        else:
            configure_backfill_runtime(args, runner)
            report = runner.run(stage=args.stage, apply_candidates=args.apply_candidates, cohort_id=args.cohort)
    print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    return 0
