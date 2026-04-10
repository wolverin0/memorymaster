"""CLI handlers — curation, wiki, vault, dream, and the master dispatch table.

The COMMAND_HANDLERS dict at the bottom of this file maps every CLI subcommand
to its handler. cli.main() imports this dict and dispatches to it.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

from memorymaster.cli_handlers_basic import (
    _handle_check_staleness,
    _handle_compact,
    _handle_compact_summaries,
    _handle_context,
    _handle_dedup,
    _handle_export_metrics,
    _handle_history,
    _handle_ingest,
    _handle_init_db,
    _handle_link_commands,
    _handle_list_claims,
    _handle_list_events,
    _handle_pin,
    _handle_qdrant_commands,
    _handle_query,
    _handle_ready,
    _handle_recompute_tiers,
    _handle_redact_claim,
    _handle_resolve_conflicts,
    _handle_resolve_proposal,
    _handle_review_queue,
    _handle_run_cycle,
    _handle_run_daemon,
    _handle_run_dashboard,
    _handle_run_operator,
    _handle_run_steward,
    _handle_snapshot_commands,
    _handle_stealth_status,
    _handle_steward_proposals,
)
from memorymaster.cli_helpers import (
    _claim_to_dict,
    _json_envelope,
    _json_error,
    _resolve_claim_id,
)
from memorymaster.service import MemoryService


def _handle_export_vault(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.vault_exporter import export_vault
    t0 = time.perf_counter()
    result = export_vault(service.store, output_dir=args.output, scope_filter=args.scope or None, confirmed_only=args.confirmed_only, include_archived=args.include_archived, incremental=args.incremental)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        print(f"Exported {result['exported']} claims to {args.output} ({result['directories_created']} dirs, {elapsed_ms:.0f}ms)")
    return 0


def _handle_curate_vault(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.vault_curator import curate_vault
    t0 = time.perf_counter()
    result = curate_vault(effective_db, output_dir=args.output, scope_filter=args.scope or None, dry_run=args.dry_run)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        label = "[DRY RUN] " if args.dry_run else ""
        print(f"{label}Curated {result['claims']} claims -> {result['files_written']} files, {result['scopes']} scopes, {result['topics']} topics ({elapsed_ms:.0f}ms)")
        if args.dry_run and "topic_breakdown" in result:
            for topic, count in sorted(result["topic_breakdown"].items(), key=lambda x: -x[1]):
                print(f"  {topic}: {count}")
    return 0


def _handle_lint_vault(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.vault_linter import lint_vault
    from memorymaster.vault_log import log_lint
    t0 = time.perf_counter()
    report = lint_vault(effective_db, scope_filter=args.scope or None, verify_with_llm=not args.no_llm, max_stale_days=args.max_stale_days)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    log_lint(report)
    if args.json_output:
        print(_json_envelope(report, query_ms=elapsed_ms))
    else:
        print(f"Lint: {report['claims']} claims, {report['issues']} issues ({elapsed_ms:.0f}ms)")
        if report["contradictions"]:
            print(f"\n  Contradictions ({len(report['contradictions'])}):")
            for c in report["contradictions"][:10]:
                claims_str = " vs ".join(f"#{cl['id']}({cl['value'][:30]})" for cl in c["claims"][:2])
                expl = f" — {c['explanation']}" if c.get("explanation") else ""
                print(f"    {c['key']}: {claims_str}{expl}")
        if report["orphans"]:
            print(f"\n  Orphans ({len(report['orphans'])}):")
            for o in report["orphans"][:10]:
                print(f"    #{o['id']} {o.get('subject', '')} — {o['reason']}")
        if report["gaps"]:
            print(f"\n  Knowledge gaps ({len(report['gaps'])}):")
            for g in report["gaps"][:10]:
                print(f"    {g['entity']}: mentioned {g['mentions']}x but no dedicated claims")
        if report["stale"]:
            print(f"\n  Stale claims ({len(report['stale'])}):")
            for s in report["stale"][:10]:
                print(f"    #{s['id']} ({s['age_days']}d old, conf={s['confidence']:.2f}) {s['text'][:50]}")
    return 0


def _handle_wiki_absorb(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.wiki_engine import absorb
    from memorymaster.vault_log import log_curate
    t0 = time.perf_counter()
    result = absorb(effective_db, wiki_dir=args.output, scope_filter=args.scope or None)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    log_curate(result, args.output)

    # Regenerate Bases alongside absorb unless suppressed
    if not getattr(args, "no_bases", False):
        try:
            from memorymaster.vault_bases import generate_bases
            bases_result = generate_bases(args.output)
            result["bases"] = bases_result
        except Exception as e:
            result["bases_error"] = str(e)

    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        print(f"Absorbed {result['subjects']} subjects -> {result['articles_written']} new, {result['articles_updated']} updated ({elapsed_ms:.0f}ms)")
        if "bases" in result:
            print(f"Bases: {result['bases']['written']} written to {result['bases']['path']}")
    return 0


def _handle_bases_generate(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.vault_bases import generate_bases
    t0 = time.perf_counter()
    result = generate_bases(args.output)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        print(f"Bases: {result['written']} written to {result['path']} ({elapsed_ms:.0f}ms)")
        for f in result["files"]:
            print(f"  - {f}")
    return 0


def _handle_wiki_cleanup(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.wiki_engine import cleanup
    t0 = time.perf_counter()
    result = cleanup(wiki_dir=args.output, scope_filter=args.scope or None)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        print(f"Cleanup: {result['audited']} audited, {result['rewritten']} rewritten ({elapsed_ms:.0f}ms)")
    return 0


def _handle_verify_claims(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.claim_verifier import verify_claims
    t0 = time.perf_counter()
    result = verify_claims(effective_db, scope_filter=args.scope or None, limit=args.limit)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        print(f"Verified: {result['checked']} refs checked, {result['valid']} valid, {result['stale_candidates']} potentially stale ({elapsed_ms:.0f}ms)")
        if result.get("error"):
            print(f"  Error: {result['error']}")
        for issue in result.get("issues", [])[:15]:
            print(f"  #{issue['claim_id']} (conf={issue['confidence']:.2f}): {', '.join(issue['issues'])}")
            print(f"    {issue['text']}")
    return 0


def _handle_mine_transcript(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.transcript_miner import mine_transcript
    t0 = time.perf_counter()
    result = mine_transcript(args.input, effective_db, scope=args.scope, max_claims=getattr(args, 'max', 100))
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        print(f"Mined: {result['scanned']} scanned, {result['ingested']} ingested, {result['skipped']} skipped, {result['duplicates']} dupes ({elapsed_ms:.0f}ms)")
    return 0


def _handle_wiki_breakdown(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.wiki_engine import breakdown
    t0 = time.perf_counter()
    result = breakdown(effective_db, wiki_dir=args.output, scope_filter=args.scope or None)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        print(f"Breakdown: {result['missing']} missing subjects, {result['created']} created ({elapsed_ms:.0f}ms)")
    return 0


def _handle_extract_entities(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.entity_graph import EntityGraph
    t0 = time.perf_counter()
    eg = EntityGraph(str(effective_db))
    eg.ensure_tables()
    claims = service.store.find_by_status(args.status, limit=args.limit, include_citations=False)
    total_entities = 0
    for claim in claims:
        names = eg.extract_and_link(claim.id, claim.text)
        total_entities += len(names)
        if names:
            print(f"  [{claim.id}] {', '.join(names)}")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    stats = eg.get_stats()
    if args.json_output:
        print(_json_envelope({"extracted": total_entities, "claims_processed": len(claims), **stats}, query_ms=elapsed_ms))
    else:
        print(f"Extracted {total_entities} entities from {len(claims)} claims ({elapsed_ms:.0f}ms)")
        print(f"Graph: {stats['entities']} entities, {stats['edges']} edges, {stats['claim_links']} links")
    return 0


def _handle_entity_stats(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.entity_graph import EntityGraph
    eg = EntityGraph(str(effective_db))
    eg.ensure_tables()
    stats = eg.get_stats()
    if args.json_output:
        print(_json_envelope(stats))
    else:
        print(f"Entities: {stats['entities']}, Edges: {stats['edges']}, Claim links: {stats['claim_links']}")
        for t, c in stats.get('by_type', {}).items():
            print(f"  {t}: {c}")
    return 0


def _handle_feedback_stats(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.feedback import FeedbackTracker
    ft = FeedbackTracker(str(effective_db))
    ft.ensure_tables()
    stats = ft.get_stats()
    if args.json_output:
        print(_json_envelope(stats))
    else:
        print(f"Feedback rows: {stats['feedback_rows']}, Claims scored: {stats['claims_scored']}, Avg quality: {stats['avg_quality']}")
    return 0


def _handle_quality_scores(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.feedback import FeedbackTracker
    t0 = time.perf_counter()
    ft = FeedbackTracker(str(effective_db))
    ft.ensure_tables()
    result = ft.compute_quality_scores()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        print(f"Quality scores computed for {result['scored']} claims ({elapsed_ms:.0f}ms)")
    return 0


def _handle_auto_resolve(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.auto_resolver import auto_resolve_conflicts
    t0 = time.perf_counter()
    result = auto_resolve_conflicts(service.store, limit=args.limit)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        print(f"Evaluated {result['pairs_evaluated']} conflict pairs: {result['resolved']} resolved, {result['failed']} failed ({elapsed_ms:.0f}ms)")
    return 0


def _handle_train_model(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.rl_trainer import train_quality_model
    t0 = time.perf_counter()
    result = train_quality_model(str(effective_db))
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        status = result.get("status", "unknown")
        if status == "trained":
            print(f"Model trained: {result['samples']} samples, AUC={result['cv_auc_mean']:.3f}, saved to {result['model_path']}")
        elif status == "skipped":
            print(f"Training skipped: {result.get('reason', '?')} — {result.get('suggestion', '')}")
        else:
            print(f"Training failed: {result.get('reason', '?')}")
    return 0


def _handle_extract_claims(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    import sys
    from memorymaster.auto_extractor import extract_claims_from_text

    if getattr(args, "stdin", False):
        raw_text = sys.stdin.read()
    else:
        input_val = args.input or ""
        input_path = Path(input_val)
        if input_path.is_file():
            raw_text = input_path.read_text(encoding="utf-8", errors="replace")
        else:
            raw_text = input_val

    t0 = time.perf_counter()
    extracted = extract_claims_from_text(
        text=raw_text,
        source=args.source,
        scope=args.scope,
        base_url=args.ollama_url,
        model=args.model,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000

    if args.ingest:
        ingested_ids: list[int] = []
        for item in extracted:
            claim = service.ingest(
                text=item["text"],
                citations=[CitationInput(source=item["source"])],
                claim_type=item.get("claim_type"),
                subject=item.get("subject"),
                predicate=item.get("predicate"),
                object_value=item.get("object_value"),
                scope=item["scope"],
            )
            ingested_ids.append(claim.id)
        if args.json_output:
            print(_json_envelope({"extracted": len(extracted), "ingested": len(ingested_ids), "claim_ids": ingested_ids}, total=len(ingested_ids), query_ms=elapsed_ms))
        else:
            print(f"Extracted {len(extracted)} claims, ingested {len(ingested_ids)} ({elapsed_ms:.0f}ms)")
            for cid, item in zip(ingested_ids, extracted, strict=True):
                print(f"  [{cid}] {item['text'][:100]}")
    else:
        if args.json_output:
            print(_json_envelope({"extracted": len(extracted), "claims": extracted}, total=len(extracted), query_ms=elapsed_ms))
        else:
            print(f"Extracted {len(extracted)} claims [DRY RUN — use --ingest to persist] ({elapsed_ms:.0f}ms)")
            for i, item in enumerate(extracted, 1):
                print(f"  [{i}] ({item['claim_type']}) {item['text'][:100]}")
                if item.get("subject"):
                    print(f"       tuple=({item['subject']}, {item.get('predicate', '-')}, {item.get('object_value', '-')})")
    return 0


def _handle_federated_query(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    t0 = time.perf_counter()
    rows_data = service.federated_query(query_text=args.text, limit=args.limit)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        json_rows = [
            {
                "claim": _claim_to_dict(row["claim"]),
                **{k: float(row.get(k, 0.0)) for k in _SCORE_KEYS},
                "annotation": row.get("annotation", {}),
            }
            for row in rows_data
        ]
        print(_json_envelope(json_rows, total=len(json_rows), query_ms=elapsed_ms))
    else:
        for row in rows_data:
            print_claim(row["claim"])
            sc = {k: float(row.get(k, 0.0)) for k in _SCORE_KEYS}
            ann = row.get("annotation", {})
            print(
                f"  retrieval: score={sc['score']:.3f} lex={sc['lexical_score']:.3f} "
                f"conf={sc['confidence_score']:.3f} fresh={sc['freshness_score']:.3f} "
                f"vec={sc['vector_score']:.3f} "
                f"active={int(bool(ann.get('active')))} stale={int(bool(ann.get('stale')))} "
                f"conflicted={int(bool(ann.get('conflicted')))} pinned={int(bool(ann.get('pinned')))}"
            )
        print(f"rows={len(rows_data)}")
    return 0


def _handle_sessions(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    import datetime
    from memorymaster.session_tracker import SessionTracker

    t0 = time.perf_counter()
    tracker = SessionTracker(effective_db)
    sessions = tracker.get_active_sessions()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(sessions, total=len(sessions), query_ms=elapsed_ms))
    else:
        if not sessions:
            print("No active sessions in the last hour.")
        else:
            print(f"Active sessions ({len(sessions)}):")
            for s in sessions:
                started = datetime.datetime.fromtimestamp(s["session_start"]).strftime("%Y-%m-%d %H:%M:%S")
                last_act = datetime.datetime.fromtimestamp(s["last_activity"]).strftime("%H:%M:%S")
                print(
                    f"  [{s['id']}] agent={s['agent_id']} started={started} "
                    f"last_activity={last_act} "
                    f"ingested={s['claims_ingested']} queries={s['queries_made']}"
                )
    return 0


def _handle_install_gitnexus_hook(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    import shutil

    t0 = time.perf_counter()
    workspace_path = Path(args.workspace).resolve()
    hook_src = workspace_path / "scripts" / "post-commit-gitnexus.sh"
    hooks_dir = workspace_path / ".git" / "hooks"
    hook_dst = hooks_dir / "post-commit"
    elapsed_ms = (time.perf_counter() - t0) * 1000

    if not hook_src.exists():
        msg = f"hook source not found: {hook_src}"
        if args.json_output:
            print(_json_error(msg))
        else:
            print(f"error: {msg}")
        return 1

    if not hooks_dir.is_dir():
        msg = f"not a git repository (no .git/hooks at {workspace_path})"
        if args.json_output:
            print(_json_error(msg))
        else:
            print(f"error: {msg}")
        return 1

    shutil.copy2(str(hook_src), str(hook_dst))
    hook_dst.chmod(hook_dst.stat().st_mode | 0o111)

    if args.json_output:
        print(_json_envelope({"installed": True, "path": str(hook_dst)}, query_ms=elapsed_ms))
    else:
        print(f"GitNexus post-commit hook installed: {hook_dst}")
    return 0


def _handle_recall(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.context_hook import recall as _recall
    output = _recall(args.query, db_path=str(effective_db), budget=args.budget, format=args.output_format)
    if output:
        print(output)
    else:
        print("(no relevant context found)")
    return 0


def _handle_observe(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.context_hook import observe as _observe, observe_llm
    if args.llm:
        result = observe_llm(args.text, source=args.source, db_path=str(effective_db), scope=args.scope)
        if args.json_output:
            print(_json_envelope(result))
        else:
            print(f"LLM extracted {result['extracted']} claims, ingested {result['ingested']}")
    else:
        result = _observe(args.text, source=args.source, db_path=str(effective_db), scope=args.scope, force=args.force)
        if args.json_output:
            print(_json_envelope(result))
        else:
            if result['ingested']:
                print(f"Observed: [{result['claim_type']}] claim_id={result['claim_id']}")
            else:
                print(f"Skipped: {result.get('reason', 'not memorable')}")
    return 0


def _handle_merge_db(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.db_merge import merge_databases
    t0 = time.perf_counter()
    result = merge_databases(str(effective_db), args.source)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        print(f"Merged: {result['merged']} new claims from {args.source} ({result['skipped']} skipped, {result['errors']} errors, {elapsed_ms:.0f}ms)")
    return 0




# Dispatch table: maps subcommand name -> handler(args, service, parser, effective_db) -> int
# Commands that run before MemoryService is constructed are handled first in main().
COMMAND_HANDLERS: dict[str, object] = {
    "stealth-status": _handle_stealth_status,
    "export-metrics": _handle_export_metrics,
    "init-db": _handle_init_db,
    "ingest": _handle_ingest,
    "run-cycle": _handle_run_cycle,
    "query": _handle_query,
    "context": _handle_context,
    "pin": _handle_pin,
    "redact-claim": _handle_redact_claim,
    "compact": _handle_compact,
    "compact-summaries": _handle_compact_summaries,
    "dedup": _handle_dedup,
    "recompute-tiers": _handle_recompute_tiers,
    "list-claims": _handle_list_claims,
    "list-events": _handle_list_events,
    "history": _handle_history,
    "review-queue": _handle_review_queue,
    "run-daemon": _handle_run_daemon,
    "run-dashboard": _handle_run_dashboard,
    "run-operator": _handle_run_operator,
    "run-steward": _handle_run_steward,
    "steward-proposals": _handle_steward_proposals,
    "resolve-proposal": _handle_resolve_proposal,
    "ready": _handle_ready,
    "resolve-conflicts": _handle_resolve_conflicts,
    "check-staleness": _handle_check_staleness,
    "link": _handle_link_commands,
    "unlink": _handle_link_commands,
    "links": _handle_link_commands,
    "snapshot": _handle_snapshot_commands,
    "snapshots": _handle_snapshot_commands,
    "rollback": _handle_snapshot_commands,
    "diff": _handle_snapshot_commands,
    "install-hook": _handle_snapshot_commands,
    "qdrant-sync": _handle_qdrant_commands,
    "qdrant-search": _handle_qdrant_commands,
    "export-vault": _handle_export_vault,
    "curate-vault": _handle_curate_vault,
    "lint-vault": _handle_lint_vault,
    "wiki-absorb": _handle_wiki_absorb,
    "wiki-cleanup": _handle_wiki_cleanup,
    "wiki-breakdown": _handle_wiki_breakdown,
    "bases-generate": _handle_bases_generate,
    "mine-transcript": _handle_mine_transcript,
    "verify-claims": _handle_verify_claims,
    "extract-entities": _handle_extract_entities,
    "entity-stats": _handle_entity_stats,
    "feedback-stats": _handle_feedback_stats,
    "quality-scores": _handle_quality_scores,
    "auto-resolve": _handle_auto_resolve,
    "train-model": _handle_train_model,
    "extract-claims": _handle_extract_claims,
    "federated-query": _handle_federated_query,
    "sessions": _handle_sessions,
    "install-gitnexus-hook": _handle_install_gitnexus_hook,
    "recall": _handle_recall,
    "observe": _handle_observe,
    "merge-db": _handle_merge_db,
}


def _handle_daily_note(args, service, parser, effective_db) -> int:
    from memorymaster.daily_notes import generate_daily_note, export_daily_note_md
    date = args.date or None
    if args.output:
        path = export_daily_note_md(str(effective_db), args.output, date)
        if args.json_output:
            print(_json_envelope({"path": path}))
        else:
            print(f"Daily note saved: {path}")
    else:
        result = generate_daily_note(str(effective_db), date)
        if args.json_output:
            print(_json_envelope(result))
        else:
            print(result["note"])
    return 0


def _handle_ghost_notes(args, service, parser, effective_db) -> int:
    from memorymaster.daily_notes import find_ghost_notes
    ghosts = find_ghost_notes(str(effective_db))
    if args.json_output:
        print(_json_envelope({"ghost_notes": ghosts, "count": len(ghosts)}))
    else:
        if not ghosts:
            print("No ghost notes found (all queried topics have sufficient claims)")
        else:
            print(f"Ghost Notes ({len(ghosts)} knowledge gaps):")
            for g in ghosts:
                icon = "?" if g["status"] == "ghost" else "~"
                print(f"  {icon} [[{g['topic']}]] — queried {g['query_references']}x, {g['existing_claims']} claims ({g['status']})")
    return 0


COMMAND_HANDLERS["daily-note"] = _handle_daily_note
COMMAND_HANDLERS["ghost-notes"] = _handle_ghost_notes


def _handle_dream_seed(args, service, parser, effective_db) -> int:
    from memorymaster.dream_bridge import dream_seed
    t0 = time.perf_counter()
    result = dream_seed(
        db_path=str(effective_db),
        project_path=args.project,
        min_tier=args.min_tier,
        min_quality=args.min_quality,
        max_memories=getattr(args, "max", 50),
        dry_run=args.dry_run,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        if result.get("error"):
            print(f"Error: {result['error']}")
            return 1
        tag = "DRY RUN" if result.get("dry_run") else "APPLIED"
        print(f"dream-seed [{tag}]: seeded={result['seeded']} skipped={result['skipped']} "
              f"total_claims={result['total_claims']}\n  memory_dir: {result['memory_dir']}")
    return 0


def _handle_dream_ingest(args, service, parser, effective_db) -> int:
    from memorymaster.dream_bridge import dream_ingest
    t0 = time.perf_counter()
    result = dream_ingest(db_path=str(effective_db), project_path=args.project)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        print(f"dream-ingest: ingested={result['ingested']} skipped={result['skipped']}\n"
              f"  memory_dir: {result['memory_dir']}")
    return 0


def _handle_dream_sync(args, service, parser, effective_db) -> int:
    from memorymaster.dream_bridge import dream_sync
    t0 = time.perf_counter()
    result = dream_sync(
        db_path=str(effective_db),
        project_path=args.project,
        min_tier=args.min_tier,
        min_quality=args.min_quality,
        max_memories=getattr(args, "max", 50),
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        ingest = result["ingest"]
        seed = result["seed"]
        print(f"dream-sync complete:\n"
              f"  ingest: ingested={ingest['ingested']} skipped={ingest['skipped']}\n"
              f"  seed:   seeded={seed['seeded']} skipped={seed['skipped']} total={seed['total_claims']}\n"
              f"  memory_dir: {seed.get('memory_dir', ingest.get('memory_dir', '?'))}")
    return 0


def _handle_dream_clean(args, service, parser, effective_db) -> int:
    from memorymaster.dream_bridge import dream_clean
    t0 = time.perf_counter()
    result = dream_clean(project_path=args.project, dry_run=args.dry_run)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        tag = "DRY RUN" if result.get("dry_run") else "APPLIED"
        print(f"dream-clean [{tag}]: removed={result['removed']}\n  memory_dir: {result['memory_dir']}")
        for fname in result.get("files", []):
            print(f"  - {fname}")
    return 0


COMMAND_HANDLERS["dream-seed"] = _handle_dream_seed
COMMAND_HANDLERS["dream-ingest"] = _handle_dream_ingest
COMMAND_HANDLERS["dream-sync"] = _handle_dream_sync
COMMAND_HANDLERS["dream-clean"] = _handle_dream_clean
