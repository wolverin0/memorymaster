"""CLI handlers — basic operations (claims, query, lifecycle, ops).

Half of the handlers live here; curation/wiki/dream handlers are in
`cli_handlers_curation.py`. Both modules contribute entries to the
COMMAND_HANDLERS dispatch table defined in `cli_handlers_curation.py`.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta
import json
import os
from dataclasses import asdict
from pathlib import Path
import sys
import time

from memorymaster.surfaces.cli_helpers import (
    STEALTH_DB_NAME,
    _SCORE_KEYS,
    _claim_to_dict,
    _event_to_timeline_entry,
    _json_default,
    _json_envelope,
    _json_error,
    _print_claim_brief,
    _resolve_claim_id,
    _score_str_from_payload,
    _stealth_active,
    parse_citation,
    parse_scope_allowlist,
    print_claim,
)
from memorymaster.govern.scheduler import run_daemon
from memorymaster.security import resolve_allow_sensitive_access


def _handle_create_snapshot(args: argparse.Namespace, db_resolved: Path) -> int:
    """Handle snapshot creation."""
    from memorymaster.snapshot import create_snapshot

    t0 = time.perf_counter()
    info = create_snapshot(db_resolved, workspace_root=Path(args.workspace).resolve(), message=args.message)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(asdict(info), query_ms=elapsed_ms))
    else:
        print(f"snapshot created: {info.snapshot_id}\n"
              f"  commit: {info.commit_hash or '(no git)'}\n"
              f"  time:   {info.timestamp}"
              + (f"\n  msg:    {info.message}" if info.message else "")
              + f"\n  size:   {info.size_bytes} bytes\n  path:   {info.path}")
    return 0


def _handle_list_snapshots(db_resolved: Path, args: argparse.Namespace) -> int:
    """Handle listing snapshots."""
    from memorymaster.snapshot import list_snapshots

    t0 = time.perf_counter()
    snaps = list_snapshots(db_resolved)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    items = [asdict(s) for s in snaps]
    if args.json_output:
        print(_json_envelope(items, total=len(items), query_ms=elapsed_ms))
    else:
        if not snaps:
            print("no snapshots found")
        else:
            for s in snaps:
                print(f"  {s.snapshot_id}  {s.commit_hash[:8] if s.commit_hash else '(no git)'}  {s.timestamp}  {s.size_bytes}b{f'  {s.message}' if s.message else ''}")
            print(f"\n{len(snaps)} snapshot(s)")
    return 0


def _handle_rollback(args: argparse.Namespace, db_resolved: Path) -> int:
    """Handle snapshot rollback."""
    from memorymaster.snapshot import rollback

    if not args.yes:
        try:
            answer = input(f"Restore DB from snapshot '{args.snapshot_id}'? A pre-rollback backup will be created. [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("rollback cancelled")
            return 0
    t0 = time.perf_counter()
    info = rollback(db_resolved, args.snapshot_id)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        payload = {**asdict(info), "restored_snapshot_id": info.snapshot_id}
        print(_json_envelope(payload, query_ms=elapsed_ms))
    else:
        print(f"restored from snapshot: {info.snapshot_id}\n  commit: {info.commit_hash or '(no git)'}\n  time:   {info.timestamp}")
    return 0


def _handle_diff(args: argparse.Namespace, db_resolved: Path) -> int:
    """Handle snapshot diff."""
    from memorymaster.snapshot import diff_snapshot

    t0 = time.perf_counter()
    result = diff_snapshot(db_resolved, args.snapshot_id)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(asdict(result), query_ms=elapsed_ms))
    else:
        s = result.summary
        print(f"diff vs {result.snapshot_id}: +{s['added']} added, -{s['removed']} removed, "
              f"~{s['changed']} changed, ={s['unchanged']} unchanged")
        for item in result.added:
            print(f"  + [{item['id']}] {item['status']}: {item['text'][:80]}")
        for item in result.removed:
            print(f"  - [{item['id']}] {item['status']}: {item['text'][:80]}")
        for item in result.changed:
            changes = ", ".join(f"{k}: {v['old']!r}->{v['new']!r}" for k, v in item["changes"].items())
            print(f"  ~ [{item['id']}] {changes}")
    return 0


def _handle_install_hook(args: argparse.Namespace) -> int:
    """Handle git hook installation."""
    from memorymaster.snapshot import install_git_hook

    t0 = time.perf_counter()
    result = install_git_hook(Path(args.workspace).resolve())
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        if result["installed"]:
            print(f"post-commit hook {'appended to existing' if result.get('appended') else 'created'}: {result['path']}")
        else:
            print(f"hook not installed: {result.get('reason', 'unknown')}")
    return 0


def _handle_snapshot_commands(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    """Handle snapshot, snapshots, rollback, diff, and install-hook subcommands."""
    db_resolved = Path(effective_db).resolve()

    if args.command == "snapshot":
        return _handle_create_snapshot(args, db_resolved)

    if args.command == "snapshots":
        return _handle_list_snapshots(db_resolved, args)

    if args.command == "rollback":
        return _handle_rollback(args, db_resolved)

    if args.command == "diff":
        return _handle_diff(args, db_resolved)

    if args.command == "install-hook":
        return _handle_install_hook(args)


def _handle_qdrant_commands(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str = "") -> int:
    """Handle qdrant-sync and qdrant-search subcommands."""
    from memorymaster.recall.qdrant_backend import QdrantBackend

    qdrant_url = args.qdrant_url or os.environ.get("QDRANT_URL") or ""
    ollama_url = args.ollama_url or os.environ.get("OLLAMA_URL") or ""
    qdrant_kw = {k: v for k, v in [("qdrant_url", qdrant_url), ("ollama_url", ollama_url)] if v}
    backend = QdrantBackend(**qdrant_kw)
    t0 = time.perf_counter()

    if args.command == "qdrant-sync":
        result = backend.sync_all(service.store)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if args.json_output:
            print(_json_envelope(result, query_ms=elapsed_ms))
        else:
            print(f"Qdrant sync: {result['synced']}/{result['total']} synced, {result['errors']} errors ({elapsed_ms:.0f}ms)")
        return 0

    # qdrant-search
    states = [s.strip() for s in args.states.split(",") if s.strip()] or None
    results = backend.search(args.text, limit=args.limit, min_confidence=args.min_confidence, states=states)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope({"results": results, "count": len(results)}, query_ms=elapsed_ms))
    else:
        if not results:
            print("No results found.")
        for hit in results:
            _pl = hit.get("payload", {})
            print(f"[{hit.get('claim_id', '?')}] score={hit.get('score', 0.0):.3f} "
                  f"state={_pl.get('state', '?')} conf={_pl.get('confidence', 0.0):.2f} {_pl.get('claim_text', '')[:100]}")
    return 0


def _handle_link_commands(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str = "") -> int:
    """Handle link, unlink, and links subcommands."""
    if args.command == "link":
        t0 = time.perf_counter()
        link = service.add_claim_link(_resolve_claim_id(service, args.source_id), _resolve_claim_id(service, args.target_id), args.link_type)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if args.json_output:
            print(_json_envelope(asdict(link), query_ms=elapsed_ms))
        else:
            print(f"linked claim {link.source_id} -> {link.target_id} ({link.link_type}) id={link.id}")
        return 0

    if args.command == "unlink":
        t0 = time.perf_counter()
        src_id = _resolve_claim_id(service, args.source_id)
        tgt_id = _resolve_claim_id(service, args.target_id)
        removed = service.remove_claim_link(src_id, tgt_id, args.link_type)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if args.json_output:
            print(_json_envelope({"removed": removed, "source_id": src_id, "target_id": tgt_id}, query_ms=elapsed_ms))
        else:
            print(f"removed {removed} link(s) between {src_id} and {tgt_id}")
        return 0

    if args.command == "links":
        t0 = time.perf_counter()
        resolved_id = _resolve_claim_id(service, args.claim_id)
        links = service.get_claim_links(resolved_id)
        if args.link_type:
            links = [lnk for lnk in links if lnk.link_type == args.link_type]
        elapsed_ms = (time.perf_counter() - t0) * 1000
        items = [asdict(lnk) for lnk in links]
        if args.json_output:
            print(_json_envelope({"rows": len(items), "links": items}, total=len(items), query_ms=elapsed_ms))
        else:
            print(json.dumps({"rows": len(items), "links": items}, indent=2))
        return 0


def _handle_query_paths(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str = "") -> int:
    """Handle the query-paths subcommand: BFS path query over claim links."""
    t0 = time.perf_counter()
    rows = service.query_claim_paths(
        args.claim_id,
        edge_type=getattr(args, "edge_type", None),
        direction=getattr(args, "direction", "both"),
        max_hops=getattr(args, "max_hops", 2),
        include_stale=getattr(args, "include_stale", False),
        include_conflicted=getattr(args, "include_conflicted", False),
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope({"rows": len(rows), "paths": rows}, total=len(rows), query_ms=elapsed_ms))
        return 0
    if not rows:
        print(f"No paths found from claim {args.claim_id}.")
        return 0
    print(f"claim {args.claim_id} ({getattr(args, 'direction', 'both')}, max {getattr(args, 'max_hops', 2)} hops)")
    for row in rows:
        claim = row["claim"]
        chain = " > ".join(row.get("edge_chain", [])) or "?"
        text = str(claim.get("text", ""))[:80]
        print(f"{'  ' * row['depth']}|-[{chain}] #{claim.get('id')} "
              f"(conf={row.get('path_confidence', 0.0):.2f}) {text}")
    return 0


def _handle_stealth_status(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    t0 = time.perf_counter()
    stealth_path = Path.cwd() / STEALTH_DB_NAME
    active = _stealth_active(args)
    db_display = str(Path(effective_db).resolve()) if "://" not in effective_db else effective_db
    elapsed_ms = (time.perf_counter() - t0) * 1000
    info = {"stealth_active": active, "stealth_db_exists": stealth_path.exists(),
            "stealth_db_path": str(stealth_path.resolve()), "effective_db": db_display,
            "gitignore_hint": f"Add '{STEALTH_DB_NAME}' to your .gitignore"}
    if args.json_output:
        print(_json_envelope(info, query_ms=elapsed_ms))
    else:
        print(f"stealth mode: {'ACTIVE' if active else 'inactive'}\n"
              f"stealth db exists: {stealth_path.exists()}\n"
              f"stealth db path: {stealth_path.resolve()}\n"
              f"effective db: {db_display}")
        if not active:
            print("\nTip: run 'memorymaster --stealth init-db' to create a stealth DB here.")
        print(f"\nRemember to add '{STEALTH_DB_NAME}' to your .gitignore")
    return 0


def _handle_export_metrics(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.surfaces.metrics_exporter import export_metrics

    snapshot = export_metrics(events_jsonl=[Path(p) for p in args.events_jsonl],
        out_prom=Path(args.out_prom), out_json=Path(args.out_json))
    c = snapshot.get("counters", {})
    print(json.dumps({"command": "export-metrics",
        "events_total": int(c.get("events_total", 0)), "transitions_total": int(c.get("transitions_total", 0)),
        "status_total": int(c.get("status_total", 0)),
        "out_prom": args.out_prom, "out_json": args.out_json}, indent=2))
    return 0


def _parse_mcp_usage_since(value: str) -> datetime:
    cleaned = value.strip()
    if cleaned.endswith("d") and cleaned[:-1].isdigit():
        return datetime.utcnow() - timedelta(days=int(cleaned[:-1]))
    return datetime.fromisoformat(cleaned.replace("Z", "+00:00"))


def handle_mcp_usage_report(args: argparse.Namespace, db_path) -> int:
    if args.format != "csv":
        raise ValueError("only csv format is supported")

    from memorymaster.surfaces.mcp_usage import query_window

    rows = query_window(db_path, _parse_mcp_usage_since(args.since))
    fieldnames = ["tool_name", "timestamp", "latency_ms", "tenant_id", "result_status"]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name) for name in fieldnames})
    return 0


def _handle_init_db(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.bridges.atlas_contract import atlas_meta

    t0 = time.perf_counter()
    service.init_db()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    db_path = effective_db if "://" in effective_db else str(Path(effective_db).resolve())
    if args.json_output:
        print(_json_envelope(
            {"db": db_path, "stealth": _stealth_active(args)},
            query_ms=elapsed_ms,
            extra_meta=atlas_meta("init-db"),
        ))
    else:
        print(f"initialized db: {db_path}{' (stealth)' if _stealth_active(args) else ''}")
    return 0


def _handle_ingest(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    citations = [parse_citation(raw) for raw in args.source]
    t0 = time.perf_counter()
    claim = service.ingest(
        text=args.text, citations=citations, idempotency_key=args.idempotency_key,
        claim_type=args.claim_type, subject=args.subject, predicate=args.predicate,
        object_value=args.object_value, scope=args.scope, volatility=args.volatility, confidence=args.confidence,
        event_time=args.event_time, valid_from=args.valid_from, valid_until=args.valid_until,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(_claim_to_dict(claim), total=1, query_ms=elapsed_ms))
    else:
        hid = getattr(claim, "human_id", None) or ""
        print(f"ingested claim_id={claim.id}{f' human_id={hid}' if hid else ''} status={claim.status} citations={len(claim.citations)}")
    return 0


def _handle_ingest_daydream(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.govern.jobs.daydream_ingest import ingest_insights

    t0 = time.perf_counter()
    result = ingest_insights(
        service,
        Path(args.insights_dir),
        min_score=args.min_score,
        scope=args.scope,
        dry_run=args.dry_run,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        tag = " [DRY RUN]" if args.dry_run else ""
        print(
            f"ingest-daydream{tag}: ingested={result['ingested']} "
            f"skipped={result['skipped']} errors={len(result['errors'])}"
        )
    return 0


def _handle_import_whatsapp(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.bridges.atlas_contract import atlas_meta
    from memorymaster.bridges.connectors.whatsapp import import_wacli_json

    t0 = time.perf_counter()
    result = import_wacli_json(
        service,
        args.input,
        display_name=args.display_name,
        chat_id=args.chat_id,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    payload = result.to_dict()
    if args.json_output:
        print(_json_envelope(
            payload,
            total=result.source_items_seen,
            query_ms=elapsed_ms,
            extra_meta=atlas_meta("import-whatsapp"),
        ))
    else:
        print(
            f"whatsapp import complete: seen={result.source_items_seen} "
            f"imported={result.source_items_imported} updated={result.source_items_updated} "
            f"evidence_added={result.evidence_items_added} duplicates={result.duplicates_seen}"
        )
    return 0


def _handle_propose_actions(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.bridges.action_extractor import propose_actions_from_evidence
    from memorymaster.bridges.atlas_contract import atlas_meta

    t0 = time.perf_counter()
    result = propose_actions_from_evidence(service, destination=args.destination, limit=args.limit)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    payload = result.to_dict()
    if args.json_output:
        print(_json_envelope(
            payload,
            total=result.created,
            query_ms=elapsed_ms,
            extra_meta=atlas_meta("propose-actions"),
        ))
    else:
        print(
            f"action proposals: scanned={result.scanned} matched={result.matched} "
            f"created={result.created} existing={result.existing}"
        )
        for proposal in result.proposals[:20]:
            due = f" due={proposal.suggested_due_at}" if proposal.suggested_due_at else ""
            print(f"  #{proposal.id} [{proposal.status}] {proposal.title}{due}")
    return 0


def _handle_extract_atlas_claims(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.bridges.atlas_claim_extractor import extract_atlas_claims_from_evidence
    from memorymaster.bridges.atlas_contract import atlas_meta

    t0 = time.perf_counter()
    result = extract_atlas_claims_from_evidence(service, scope=args.scope, limit=args.limit)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    payload = result.to_dict()
    if args.json_output:
        print(_json_envelope(
            payload,
            total=result.ingested,
            query_ms=elapsed_ms,
            extra_meta=atlas_meta("extract-atlas-claims"),
        ))
    else:
        print(
            f"atlas claims: scanned={result.scanned} matched={result.matched} "
            f"ingested={result.ingested}"
        )
        for claim in result.claims[:20]:
            print(f"  #{claim.id} [{claim.status}] {claim.text}")
    return 0


def _handle_action_proposals(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.bridges.atlas_contract import atlas_meta

    t0 = time.perf_counter()
    proposals = service.list_action_proposals(
        status=args.status,
        destination=args.destination,
        limit=args.limit,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    payload = [asdict(proposal) for proposal in proposals]
    if args.json_output:
        print(_json_envelope(
            payload,
            total=len(payload),
            query_ms=elapsed_ms,
            extra_meta=atlas_meta("action-proposals"),
        ))
    else:
        for proposal in proposals:
            due = f" due={proposal.suggested_due_at}" if proposal.suggested_due_at else ""
            print(f"#{proposal.id} [{proposal.status}] {proposal.destination} {proposal.title}{due}")
        print(f"{len(proposals)} proposal(s)")
    return 0


def _handle_resolve_action_proposal(
    args: argparse.Namespace,
    service,
    parser: argparse.ArgumentParser,
    effective_db: str,
) -> int:
    from memorymaster.bridges.atlas_contract import atlas_meta

    t0 = time.perf_counter()
    proposal = service.update_action_proposal_status(
        args.proposal_id,
        status=args.status,
        external_ref=args.external_ref,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(
            asdict(proposal),
            total=1,
            query_ms=elapsed_ms,
            extra_meta=atlas_meta("resolve-action-proposal"),
        ))
    else:
        print(f"proposal #{proposal.id} status={proposal.status} external_ref={proposal.external_ref or ''}")
    return 0


def _handle_transcribe_source_item(
    args: argparse.Namespace,
    service,
    parser: argparse.ArgumentParser,
    effective_db: str,
) -> int:
    from memorymaster.bridges.atlas_contract import atlas_meta
    from memorymaster.bridges.media_processing import process_transcription
    from memorymaster.bridges.media_providers import get_transcription_provider

    provider = get_transcription_provider(args.provider)
    t0 = time.perf_counter()
    outcome = process_transcription(service, args.source_item_id, provider)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    payload = {
        "source_item_id": outcome.source_item_id,
        "created": outcome.created,
        "evidence": asdict(outcome.evidence) if outcome.evidence else None,
        "error": outcome.error,
        "provider": provider.provider_name,
    }
    if args.json_output:
        print(_json_envelope(
            payload,
            total=1 if outcome.evidence else 0,
            query_ms=elapsed_ms,
            extra_meta=atlas_meta("transcribe-source-item"),
        ))
    else:
        if outcome.evidence:
            label = "created" if outcome.created else "existing"
            print(f"transcript {label}: evidence #{outcome.evidence.id} provider={provider.provider_name} "
                  f"len={len(outcome.evidence.text or '')}")
        else:
            print(f"transcription failed via {provider.provider_name}: {outcome.error}")
    return 0


def _handle_ocr_source_item(
    args: argparse.Namespace,
    service,
    parser: argparse.ArgumentParser,
    effective_db: str,
) -> int:
    from memorymaster.bridges.atlas_contract import atlas_meta
    from memorymaster.bridges.media_processing import process_ocr
    from memorymaster.bridges.media_providers import get_ocr_provider

    provider = get_ocr_provider(args.provider)
    t0 = time.perf_counter()
    outcome = process_ocr(service, args.source_item_id, provider)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    payload = {
        "source_item_id": outcome.source_item_id,
        "created": outcome.created,
        "evidence": asdict(outcome.evidence) if outcome.evidence else None,
        "error": outcome.error,
        "provider": provider.provider_name,
    }
    if args.json_output:
        print(_json_envelope(
            payload,
            total=1 if outcome.evidence else 0,
            query_ms=elapsed_ms,
            extra_meta=atlas_meta("ocr-source-item"),
        ))
    else:
        if outcome.evidence:
            label = "created" if outcome.created else "existing"
            print(f"ocr {label}: evidence #{outcome.evidence.id} provider={provider.provider_name} "
                  f"len={len(outcome.evidence.text or '')}")
        else:
            print(f"ocr failed via {provider.provider_name}: {outcome.error}")
    return 0


def _handle_enqueue_media_retry(
    args: argparse.Namespace,
    service,
    parser: argparse.ArgumentParser,
    effective_db: str,
) -> int:
    from memorymaster.bridges.atlas_contract import atlas_meta

    t0 = time.perf_counter()
    item = service.enqueue_media_retry(
        source_item_id=args.source_item_id,
        media_key=args.media_key,
        chat_id=args.chat_id,
        media_type=args.media_type,
        media_path=args.media_path,
        media_url=args.media_url,
        next_attempt_time=args.next_attempt_time,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(
            asdict(item),
            total=1,
            query_ms=elapsed_ms,
            extra_meta=atlas_meta("enqueue-media-retry"),
        ))
    else:
        print(f"retry #{item.id} status={item.status} attempts={item.attempt_count} key={item.media_key}")
    return 0


def _handle_process_media_retry_queue(
    args: argparse.Namespace,
    service,
    parser: argparse.ArgumentParser,
    effective_db: str,
) -> int:
    from memorymaster.bridges.atlas_contract import atlas_meta

    t0 = time.perf_counter()
    claimed = service.claim_pending_media_retries(limit=args.limit)
    counts = service.media_retry_status_counts()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    payload = {
        "attempted": len(claimed),
        "expired": counts.get("expired", 0),
        "recovered": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "pending_remaining": counts.get("pending", 0),
        "rows": [asdict(r) for r in claimed],
    }
    if args.json_output:
        print(_json_envelope(
            payload,
            total=payload["attempted"],
            query_ms=elapsed_ms,
            extra_meta=atlas_meta("process-media-retry-queue"),
        ))
    else:
        print(
            f"media-retry tick: attempted={payload['attempted']} "
            f"expired={payload['expired']} recovered={payload['recovered']} "
            f"failed={payload['failed']} pending_remaining={payload['pending_remaining']}"
        )
        for r in claimed:
            print(f"  retry #{r.id} attempt={r.attempt_count} key={r.media_key} src={r.source_item_id}")
    return 0


def _handle_record_media_retry_outcome(
    args: argparse.Namespace,
    service,
    parser: argparse.ArgumentParser,
    effective_db: str,
) -> int:
    from memorymaster.bridges.atlas_contract import atlas_meta

    t0 = time.perf_counter()
    item = service.record_media_retry_outcome(
        args.retry_id,
        status=args.status,
        media_path=args.media_path,
        last_http_status=args.last_http_status,
        last_error=args.last_error,
        next_attempt_time=args.next_attempt_time,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(
            asdict(item),
            total=1,
            query_ms=elapsed_ms,
            extra_meta=atlas_meta("record-media-retry-outcome"),
        ))
    else:
        print(f"retry #{item.id} status={item.status} attempts={item.attempt_count} http={item.last_http_status or '-'}")
    return 0


def _handle_list_media_retries(
    args: argparse.Namespace,
    service,
    parser: argparse.ArgumentParser,
    effective_db: str,
) -> int:
    from memorymaster.bridges.atlas_contract import atlas_meta

    t0 = time.perf_counter()
    rows = service.list_media_retries(
        status=args.status,
        source_item_id=args.source_item_id,
        limit=args.limit,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    payload = [asdict(r) for r in rows]
    if args.json_output:
        print(_json_envelope(
            payload,
            total=len(payload),
            query_ms=elapsed_ms,
            extra_meta=atlas_meta("list-media-retries"),
        ))
    else:
        for r in rows:
            print(f"#{r.id} [{r.status}] attempts={r.attempt_count} src={r.source_item_id} key={r.media_key}")
        print(f"{len(rows)} retry row(s)")
    return 0


def _handle_label_source_item(
    args: argparse.Namespace,
    service,
    parser: argparse.ArgumentParser,
    effective_db: str,
) -> int:
    from memorymaster.bridges.atlas_contract import atlas_meta

    sensitivity = None if args.sensitivity == "clear" else args.sensitivity
    t0 = time.perf_counter()
    item = service.set_source_item_sensitivity(args.source_item_id, sensitivity)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(
            asdict(item),
            total=1,
            query_ms=elapsed_ms,
            extra_meta=atlas_meta("label-source-item"),
        ))
    else:
        print(f"source_item #{item.id} sensitivity={item.sensitivity or '(none)'}")
    return 0


def _handle_label_evidence_item(
    args: argparse.Namespace,
    service,
    parser: argparse.ArgumentParser,
    effective_db: str,
) -> int:
    from memorymaster.bridges.atlas_contract import atlas_meta

    sensitivity = None if args.sensitivity == "clear" else args.sensitivity
    t0 = time.perf_counter()
    item = service.set_evidence_item_sensitivity(args.evidence_item_id, sensitivity)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(
            asdict(item),
            total=1,
            query_ms=elapsed_ms,
            extra_meta=atlas_meta("label-evidence-item"),
        ))
    else:
        print(f"evidence_item #{item.id} sensitivity={item.sensitivity or '(none)'}")
    return 0


def _handle_edit_action_proposal(
    args: argparse.Namespace,
    service,
    parser: argparse.ArgumentParser,
    effective_db: str,
) -> int:
    from memorymaster.bridges.atlas_contract import atlas_meta

    t0 = time.perf_counter()
    proposal = service.update_action_proposal_fields(
        args.proposal_id,
        title=args.title,
        description=args.description,
        suggested_due_at=args.suggested_due_at,
        confidence=args.confidence,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(
            asdict(proposal),
            total=1,
            query_ms=elapsed_ms,
            extra_meta=atlas_meta("edit-action-proposal"),
        ))
    else:
        print(f"proposal #{proposal.id} title='{proposal.title}' due={proposal.suggested_due_at or '-'} confidence={proposal.confidence:.2f}")
    return 0


def _handle_export_actions(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.bridges.action_exporters import export_approved_actions
    from memorymaster.bridges.atlas_contract import atlas_meta

    t0 = time.perf_counter()
    result = export_approved_actions(
        service,
        args.output,
        destination=args.destination,
        limit=args.limit,
        mark_exported=not args.dry_run,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    payload = result.to_dict()
    if args.json_output:
        print(_json_envelope(
            payload,
            total=result.exported,
            query_ms=elapsed_ms,
            extra_meta=atlas_meta("export-actions"),
        ))
    else:
        label = "prepared" if args.dry_run else "exported"
        print(f"{label} {result.exported} action(s) -> {result.output_path}")
    return 0


def _handle_atlas_version(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.bridges.atlas_contract import atlas_contract_payload, atlas_meta

    t0 = time.perf_counter()
    payload = atlas_contract_payload()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(
            payload,
            total=1,
            query_ms=elapsed_ms,
            extra_meta=atlas_meta("atlas-version"),
        ))
    else:
        print(f"atlas_contract_version={payload['atlas_contract_version']}")
        print(f"atlas_contract_name={payload['atlas_contract_name']}")
        print(f"subcommands={len(payload['subcommands'])} endpoints={len(payload['endpoints'])}")
    return 0


def _handle_run_cycle(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    t0 = time.perf_counter()
    result = service.run_cycle(run_compactor=args.with_compact, min_citations=args.min_citations,
        min_score=args.min_score, policy_mode=args.policy_mode, policy_limit=args.policy_limit)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        print(json.dumps(result, indent=2))

    if getattr(args, "with_dream_sync", False):
        from memorymaster.bridges.dream_bridge import dream_sync
        try:
            sync_result = dream_sync(effective_db, project_path=args.dream_project)
            print(f"\ndream-sync: ingested={sync_result.get('ingested', 0)} "
                  f"seeded={sync_result.get('seeded', 0)} "
                  f"skipped={sync_result.get('skipped', 0)}")
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"\ndream-sync skipped: {exc}")

    return 0


def _print_score_explanation(breakdown: dict | None) -> None:
    """Render per-stage score attribution for `query --explain`.

    Shows query-relevance vs. the metadata boost terms and whether the
    floor-ratio gate suppressed the boosts for this result.
    """
    if not breakdown:
        print("    explain: (no breakdown — legacy retrieval mode)")
        return
    terms = breakdown.get("boost_terms", {})
    w = breakdown.get("weights", (0, 0, 0, 0))
    applied = breakdown.get("boosts_applied", True)
    gate = "applied" if applied else f"GATED (relevance < floor={breakdown.get('floor', 0.0):.3f})"
    term_str = " ".join(f"{k}={v:+.3f}" for k, v in terms.items())
    print(f"    explain: relevance={breakdown.get('relevance', 0.0):.3f} "
          f"boosts={breakdown.get('boosts_total', 0.0):+.3f} [{gate}] -> final={breakdown.get('final', 0.0):.3f}")
    print(f"             weights(l,c,f,v)={tuple(round(x, 2) for x in w)}  boost_terms: {term_str}")


def _handle_query(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    allow_sensitive = resolve_allow_sensitive_access(
        allow_sensitive=args.allow_sensitive, context="cli.query"
    )
    if getattr(args, "as_of", ""):
        t0 = time.perf_counter()
        claims = service.store.query_as_of(args.as_of)
        # Parity with the non-as-of path: never surface sensitive-visibility
        # claims in plaintext unless allow_sensitive was actually granted.
        if not allow_sensitive:
            claims = [
                c for c in claims
                if (getattr(c, "visibility", "public") or "public").strip().lower() != "sensitive"
            ]
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if args.json_output:
            print(_json_envelope([_claim_to_dict(c) for c in claims], total=len(claims), query_ms=elapsed_ms))
        else:
            for c in claims:
                print_claim(c)
            print(f"rows={len(claims)}")
        return 0
    if getattr(args, "auto_classify", False):
        from memorymaster.recall.query_classifier import classify_query, recommended_retrieval_mode
        qtype = classify_query(args.text)
        retrieval_mode = recommended_retrieval_mode(qtype)
        print(f"query classified as: {qtype} → using {retrieval_mode} mode")
        args.retrieval_mode = retrieval_mode
    t0 = time.perf_counter()
    rows_data = service.query_rows(
        query_text=args.text, limit=args.limit,
        include_stale=not args.exclude_stale, include_conflicted=not args.exclude_conflicted,
        include_candidates=getattr(args, "include_candidates", False),
        retrieval_mode=args.retrieval_mode, allow_sensitive=args.allow_sensitive,
        retrieval_profile=getattr(args, "profile", None),
        scope_allowlist=parse_scope_allowlist(args.scope_allowlist),
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        json_rows = [{"claim": _claim_to_dict(row["claim"]),
            **{k: float(row.get(k, 0.0)) for k in _SCORE_KEYS},
            "annotation": row.get("annotation", {})} for row in rows_data]
        print(_json_envelope(json_rows, total=len(json_rows), query_ms=elapsed_ms))
    else:
        for row in rows_data:
            print_claim(row["claim"])
            ann = row.get("annotation", {})
            sc = {k: float(row.get(k, 0.0)) for k in _SCORE_KEYS}
            print(f"  retrieval: score={sc['score']:.3f} lex={sc['lexical_score']:.3f} "
                  f"conf={sc['confidence_score']:.3f} fresh={sc['freshness_score']:.3f} "
                  f"vec={sc['vector_score']:.3f} "
                  f"active={int(bool(ann.get('active')))} stale={int(bool(ann.get('stale')))} "
                  f"conflicted={int(bool(ann.get('conflicted')))} pinned={int(bool(ann.get('pinned')))}")
            if getattr(args, "explain", False):
                _print_score_explanation(row.get("breakdown"))
        print(f"rows={len(rows_data)}")
    return 0


def _print_recall_analysis(analysis: dict) -> None:
    """Render the recall-analysis breakdown for humans (non-JSON path)."""
    w = analysis.get("weights", {})
    rw = w.get("retrieval_weights", {})
    print(f"query: {analysis.get('query', '')!r}  mode={analysis.get('mode')}  "
          f"profile={analysis.get('profile')}  rows={analysis.get('rows', 0)}")
    print(f"weights(l,c,f,v)=({rw.get('lexical')},{rw.get('confidence')},"
          f"{rw.get('freshness')},{rw.get('vector')})  "
          f"floor_ratio={w.get('boost_floor_ratio')}  pinned_bonus={w.get('pinned_bonus')}")
    if w.get("profile_override"):
        print(f"profile_override: {w['profile_override']}")
    for rank, entry in enumerate(analysis.get("results", []), start=1):
        bd = entry.get("breakdown") or {}
        comp = bd.get("components", {})
        contrib = bd.get("contributions", {})
        gate = "GATED" if bd.get("floor_gated") else "applied"
        print(f"#{rank} claim={entry.get('claim_id')} ({entry.get('human_id')}) "
              f"score={entry.get('score', 0.0):.3f} tier={entry.get('tier')} "
              f"pinned={int(entry.get('pinned', False))}")
        print(f"    components: lex={comp.get('lexical', 0.0):.3f} "
              f"conf={comp.get('confidence', 0.0):.3f} fresh={comp.get('freshness', 0.0):.3f} "
              f"vec={comp.get('vector', 0.0):.3f}")
        print(f"    contributions: lex={contrib.get('lexical', 0.0):+.3f} "
              f"vec={contrib.get('vector', 0.0):+.3f} conf={contrib.get('confidence', 0.0):+.3f} "
              f"fresh={contrib.get('freshness', 0.0):+.3f} "
              f"tier={contrib.get('tier_bonus', 0.0):+.3f} pin={contrib.get('pinned_bonus', 0.0):+.3f}")
        print(f"    relevance={bd.get('relevance_subtotal', 0.0):.3f} "
              f"boosts={bd.get('boosts_subtotal', 0.0):+.3f} [{gate}] "
              f"-> final={bd.get('final_score', 0.0):.3f}")
    rankings = analysis.get("component_rankings", {})
    if rankings:
        print("component rankings (best-first claim ids):")
        for comp_name, ids in rankings.items():
            print(f"    {comp_name}: {ids}")


def _handle_recall_analysis(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    resolve_allow_sensitive_access(allow_sensitive=args.allow_sensitive, context="cli.recall-analysis")
    t0 = time.perf_counter()
    analysis = service.recall_analysis(
        query_text=args.query,
        limit=args.limit,
        retrieval_mode=args.mode,
        include_candidates=getattr(args, "include_candidates", False),
        retrieval_profile=getattr(args, "profile", None),
        allow_sensitive=args.allow_sensitive,
        scope_allowlist=parse_scope_allowlist(args.scope_allowlist),
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(analysis, total=analysis.get("rows", 0), query_ms=elapsed_ms))
    else:
        _print_recall_analysis(analysis)
    return 0


def _handle_context(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    resolve_allow_sensitive_access(allow_sensitive=args.allow_sensitive, context="cli.context")
    t0 = time.perf_counter()
    result = service.query_for_context(
        query=args.text, token_budget=args.budget, output_format=args.output_format,
        limit=args.limit, include_stale=not args.exclude_stale,
        include_conflicted=not args.exclude_conflicted,
        include_candidates=getattr(args, "include_candidates", False),
        retrieval_mode=args.retrieval_mode, allow_sensitive=args.allow_sensitive,
        retrieval_profile=getattr(args, "profile", None),
        scope_allowlist=parse_scope_allowlist(args.scope_allowlist),
        provider=getattr(args, "provider", None),
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(asdict(result), query_ms=elapsed_ms))
    else:
        print(result.output)
    return 0


def _handle_pin(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    claim = service.pin(_resolve_claim_id(service, args.claim_id), pin=not args.unpin)
    print(f"claim_id={claim.id} status={claim.status} pinned={int(claim.pinned)}")
    return 0


def _handle_redact_claim(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    result = service.redact_claim_payload(_resolve_claim_id(service, args.claim_id), mode=args.mode,
        redact_claim=not args.citations_only, redact_citations=not args.claims_only,
        reason=(args.reason.strip() or None), actor=args.actor)
    print(json.dumps(result, indent=2, default=_json_default))
    return 0


def _handle_compact(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.govern.jobs import compactor

    result = compactor.run(
        service.store,
        retain_days=args.retain_days,
        event_retain_days=args.event_retain_days,
        artifacts_dir=service.workspace_root / "artifacts" / "compaction",
        dry_run=args.dry_run,
    )
    if args.json_output:
        print(_json_envelope(result))
    elif args.dry_run:
        print("[DRY RUN] No claims archived, events deleted, or artifacts written.")
        print(
            f"compact [DRY RUN] candidates={result['candidate_claims']} "
            f"planned_archives={len(result['planned_archives'])}"
        )
        for item in result["planned_archives"]:
            print(f"  claim={item['claim_id']} {item['from_status']} -> {item['to_status']}")
        for artifact in result["artifact_files"]:
            print(f"  artifact: {artifact}")
    else:
        print(json.dumps(result, indent=2))
    return 0


def _handle_decay(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.govern.jobs import decay

    t0 = time.perf_counter()
    result = decay.run(
        service.store,
        limit=args.limit,
        stale_threshold=args.stale_threshold,
        dry_run=args.dry_run,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        if args.dry_run:
            print("[DRY RUN] No confidence or status changes applied.")
        print(
            f"decay [{'DRY RUN' if args.dry_run else 'APPLIED'}] processed={result['processed']} "
            f"decayed={result['decayed']} to_stale={result['to_stale']}"
        )
        for item in result.get("planned_transitions", []):
            print(
                f"  claim={item['claim_id']} {item['from_status']} -> {item['to_status']} "
                f"confidence={item['old_confidence']:.3f}->{item['new_confidence']:.3f}"
            )
    return 0


def _handle_compact_summaries(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.govern.llm_steward import _parse_api_keys
    rk = _parse_api_keys(api_key=args.api_key, api_keys=args.api_keys)
    t0 = time.perf_counter()
    result = service.compact_summaries(
        provider=args.provider, api_key=rk[0] if len(rk) == 1 else "",
        model=args.model, base_url=args.base_url, min_cluster=args.min_cluster,
        max_cluster=args.max_cluster, similarity_threshold=args.similarity_threshold,
        dry_run=args.dry_run, limit=args.limit,
        api_keys=rk if len(rk) > 1 else None, cooldown_seconds=args.cooldown,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        print(f"compact-summaries [{'DRY RUN' if result['dry_run'] else 'APPLIED'}] clusters={result['clusters_found']} "
              f"summaries={result['summaries_created']} source_claims={result['source_claims_summarized']} errors={result['errors']}")
        for d in result.get("details", []):
            _a, _ids, _sub = d.get("action", "unknown"), d.get("source_claim_ids", []), d.get("subject_hint", "")
            if _a == "summarized":
                print(f"  [{_a}] summary_id={d.get('summary_claim_id')} from {len(_ids)} claims (subject: {_sub})\n    {d.get('summary_text', '')[:120]}")
            elif _a == "would_summarize":
                print(f"  [{_a}] {len(_ids)} claims (subject: {_sub})")
            else:
                print(f"  [{_a}] {len(_ids)} claims (subject: {_sub}) {d.get('error', '')[:80]}")
    return 0


def _handle_dedup(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    t0 = time.perf_counter()
    result = service.dedup(
        threshold=args.threshold,
        min_text_overlap=args.min_text_overlap,
        dry_run=args.dry_run,
        limit=getattr(args, "limit", None),
        scope_filter=getattr(args, "scope", None),
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        print(f"dedup [{'DRY RUN' if result['dry_run'] else 'APPLIED'}] scanned={result['scanned']} "
              f"duplicates={result['duplicates_found']} archived={result['claims_archived']} threshold={result['threshold']}")
        for pair in result["pairs"]:
            print(f"  dup: keep={pair['keep_id']} archive={pair['archive_id']} sim={pair['similarity']:.4f} overlap={pair['text_overlap']:.4f}\n"
                  f"    keep:    {pair['keep_text'][:80]}\n    archive: {pair['archive_text'][:80]}")
    return 0


def _handle_recompute_tiers(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    t0 = time.perf_counter()
    counts = service.recompute_tiers()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(counts, query_ms=elapsed_ms))
    else:
        print(f"recompute-tiers: core={counts['core']} working={counts['working']} peripheral={counts['peripheral']}")
    return 0


def _handle_recompute_confidence_priors(
    args: argparse.Namespace,
    service,
    parser: argparse.ArgumentParser,
    effective_db: str,
) -> int:
    from memorymaster.govern.jobs.calibration import run as run_calibration

    t0 = time.perf_counter()
    report = run_calibration(
        service.store,
        window_days=args.window_days,
        output=Path(args.output),
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(report, total=len(report["priors"]), query_ms=elapsed_ms))
    else:
        print(
            "confidence-priors: "
            f"types={len(report['priors'])} attempts={report['total_attempts']} "
            f"validated={report['total_validated']} output={report['output']}"
        )
    return 0


def _handle_wiki_suggest_links(
    args: argparse.Namespace,
    service,
    parser: argparse.ArgumentParser,
    effective_db: str,
) -> int:
    from memorymaster.knowledge.wiki_suggest import suggest_wikilinks

    suggestions = suggest_wikilinks(
        effective_db,
        args.text,
        wiki_root=args.wiki_root,
        limit=args.limit,
        hops=args.hops,
    )
    print(json.dumps(suggestions))
    return 0


def _handle_list_claims(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    resolve_allow_sensitive_access(allow_sensitive=args.allow_sensitive, context="cli.list-claims")
    t0 = time.perf_counter()
    claims = service.list_claims(status=args.status, limit=args.limit,
        include_archived=args.include_archived, allow_sensitive=args.allow_sensitive)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope([_claim_to_dict(c) for c in claims], total=len(claims), query_ms=elapsed_ms))
    else:
        for claim in claims:
            print_claim(claim)
        print(f"rows={len(claims)}")
    return 0


def _handle_list_events(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    t0 = time.perf_counter()
    events = service.list_events(claim_id=args.claim_id, limit=args.limit, event_type=args.event_type)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(json.loads(json.dumps(events, default=_json_default)), total=len(events), query_ms=elapsed_ms))
    else:
        print(json.dumps({"rows": len(events), "events": events}, indent=2, default=_json_default))
    return 0


def _handle_history(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    t0 = time.perf_counter()
    resolved_id = _resolve_claim_id(service, args.claim_id)
    claim = service.store.get_claim(resolved_id, include_citations=False)
    if claim is None:
        if args.json_output:
            print(_json_error(f"Claim {args.claim_id} not found."))
        else:
            print(f"Error: Claim {args.claim_id} not found.")
        return 1
    events = service.list_events(claim_id=resolved_id, limit=args.limit)
    events.reverse()  # list_events returns newest-first
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope({"claim_id": args.claim_id, "status": claim.status,
            "confidence": claim.confidence, "timeline": [_event_to_timeline_entry(ev) for ev in events]},
            total=len(events), query_ms=elapsed_ms))
    else:
        print(f"=== History for claim {claim.id} [{claim.status} conf={claim.confidence:.3f}] ===\n  text: {claim.text}\n")
        for ev in events:
            transition = f"  {ev.from_status or '?'} -> {ev.to_status or '?'}" if (ev.from_status or ev.to_status) else ""
            details_str = f"  | {ev.details}" if ev.details else ""
            print(f"  {ev.created_at}  {ev.event_type:<25}{transition}{_score_str_from_payload(ev.payload_json)}{details_str}")
        print(f"\n  ({len(events)} events)")
    return 0


def _handle_review_queue(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.govern.review import build_review_queue, queue_to_dicts

    items = build_review_queue(service, limit=args.limit,
        include_stale=not args.exclude_stale, include_conflicted=not args.exclude_conflicted,
        include_sensitive=resolve_allow_sensitive_access(allow_sensitive=args.allow_sensitive, context="cli.review-queue"))
    print(json.dumps({"rows": len(items), "items": queue_to_dicts(items)}, indent=2))
    return 0


def _handle_run_daemon(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    result = run_daemon(service,
        interval_seconds=args.interval_seconds, max_cycles=args.max_cycles,
        compact_every=args.compact_every, min_citations=args.min_citations,
        min_score=args.min_score, policy_mode=args.policy_mode, policy_limit=args.policy_limit,
        git_trigger=args.git_trigger, git_check_seconds=args.git_check_seconds)
    print(json.dumps(result, indent=2))
    return 0


def _handle_run_dashboard(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.surfaces.dashboard import create_dashboard_server

    server = create_dashboard_server(db_target=effective_db, workspace_root=args.workspace,
        host=args.host, port=args.port, operator_log_jsonl=args.operator_log_jsonl)
    print(f"memorymaster dashboard listening on http://{args.host}:{args.port}/dashboard")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _handle_run_operator(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    try:
        from memorymaster.surfaces.operator import MemoryOperator, OperatorConfig
    except Exception as exc:
        print(f"error: run-operator unavailable: could not import memorymaster.surfaces.operator ({exc})")
        return 2

    def _stateful(val: str) -> str | None:
        return None if args.no_state else (val.strip() or None)

    config = OperatorConfig(
        reconcile_interval_seconds=args.reconcile_seconds,
        retrieval_mode=args.retrieval_mode, retrieval_limit=args.query_limit,
        progressive_retrieval=not args.disable_progressive_retrieval,
        tier1_limit=args.tier1_limit, tier2_limit=args.tier2_limit,
        min_citations=args.min_citations, min_score=args.min_score,
        policy_mode=args.policy_mode, policy_limit=args.policy_limit, compact_every=args.compact_every,
        max_idle_seconds=args.max_idle_seconds if args.max_idle_seconds and args.max_idle_seconds > 0 else None,
        log_jsonl_path=(args.log_jsonl.strip() or None),
        state_json_path=_stateful(args.state_json), queue_state_json_path=_stateful(args.queue_state_json),
        queue_journal_jsonl_path=_stateful(args.queue_journal_jsonl), queue_db_path=_stateful(args.queue_db),
    )
    operator = MemoryOperator(service=service, config=config)
    result = operator.run_stream(inbox_jsonl=Path(args.inbox_jsonl),
        poll_seconds=args.poll_seconds, max_events=args.max_events)
    print(json.dumps(result, indent=2, default=_json_default))
    return 0


def _handle_run_steward(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.govern.steward import run_steward

    t0 = time.perf_counter()
    result = run_steward(
        service, mode=args.mode, cadence_trigger=args.cadence_trigger,
        interval_seconds=args.interval_seconds, git_check_seconds=args.git_check_seconds,
        commit_every=args.commit_every, max_cycles=args.max_cycles,
        allow_sensitive=resolve_allow_sensitive_access(allow_sensitive=args.allow_sensitive, context="cli.run-steward"),
        apply=args.apply, max_claims=args.max_claims, max_proposals=args.max_proposals,
        max_probe_files=args.max_probe_files, max_probe_file_bytes=args.max_probe_file_bytes,
        max_tool_probes=args.max_tool_probes, probe_timeout_seconds=args.probe_timeout_seconds,
        probe_failure_threshold=args.probe_failure_threshold,
        enable_semantic_probe=not args.disable_semantic_probe,
        enable_tool_probe=not args.disable_tool_probe,
        enable_contradiction_probe=not getattr(args, "disable_contradiction_probe", False),
        artifact_path=Path(args.artifact_json),
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(json.loads(json.dumps(result, default=_json_default)), query_ms=elapsed_ms))
    else:
        print(json.dumps(result, indent=2, default=_json_default))
    return 0


def _handle_steward_proposals(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.govern.steward import list_steward_proposals

    rows = list_steward_proposals(service, limit=args.limit, include_resolved=args.include_resolved)
    print(json.dumps({"rows": len(rows), "proposals": rows}, indent=2, default=_json_default))
    return 0


def _handle_resolve_proposal(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    if args.proposal_event_id is None and args.claim_id is None:
        raise ValueError("resolve-proposal requires --proposal-event-id or --claim-id")
    from memorymaster.govern.steward import resolve_steward_proposal

    result = resolve_steward_proposal(service, action=args.action,
        proposal_event_id=args.proposal_event_id, claim_id=args.claim_id, apply_on_approve=not args.no_apply)
    print(json.dumps(result, indent=2, default=_json_default))
    return 0


def _handle_ready(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.govern.conflict_resolver import detect_conflicts

    t0 = time.perf_counter()
    limit = args.limit

    stale_claims = service.store.list_claims(status="stale", limit=limit, include_archived=False, include_citations=True)
    conflict_pairs = detect_conflicts(service.store, limit=500)
    all_candidates = service.store.list_claims(status="candidate", limit=limit * 3, include_archived=False)
    threshold = args.confidence_threshold
    low_conf_candidates = [c for c in all_candidates if c.confidence < threshold][:limit]
    elapsed_ms = (time.perf_counter() - t0) * 1000

    payload = {
        "stale": {"count": len(stale_claims), "claims": [_claim_to_dict(c) for c in stale_claims]},
        "conflicted": {"count": len(conflict_pairs), "pairs": [
            {"winner_id": p.winner.id, "loser_id": p.loser.id, "reason": p.reason,
             "key": list(p.key), "winner_text": p.winner.text[:120],
             "loser_text": p.loser.text[:120], "winner_confidence": p.winner.confidence,
             "loser_confidence": p.loser.confidence}
            for p in conflict_pairs[:limit]
        ]},
        "low_confidence": {"count": len(low_conf_candidates), "claims": [_claim_to_dict(c) for c in low_conf_candidates]},
        "total_attention": len(stale_claims) + len(conflict_pairs) + len(low_conf_candidates),
    }

    if args.json_output:
        print(_json_envelope(payload, total=payload["total_attention"], query_ms=elapsed_ms))
    else:
        total = payload["total_attention"]
        if total == 0:
            print("Nothing needs attention. All clear.")
            return 0

        print(f"=== {total} items need attention ===\n")

        if stale_claims:
            print(f"--- Stale claims ({len(stale_claims)}) ---\n  Previously confirmed but source files changed. Need re-validation.")
            for c in stale_claims:
                _print_claim_brief(c)
            print('  -> Run `memorymaster check-staleness` to review details\n')

        if conflict_pairs:
            print(f"--- Conflicted pairs ({len(conflict_pairs)}) ---\n  Same subject+predicate with different values. Need resolution.")
            for p in conflict_pairs[:limit]:
                print(f"  winner=[{p.winner.id}] vs loser=[{p.loser.id}] "
                      f"key=({p.key[0]}, {p.key[1]}) reason={p.reason}")
            print('  -> Run `memorymaster resolve-conflicts` to auto-resolve\n')

        if low_conf_candidates:
            print(f"--- Low-confidence candidates ({len(low_conf_candidates)}) ---\n  Candidates with confidence < {threshold}. Need more evidence or review.")
            for c in low_conf_candidates:
                _print_claim_brief(c)
            print('  -> Run `memorymaster run-cycle` to re-evaluate candidates\n')

    return 0


def _handle_entity_graph_export(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.govern.jobs.entity_graph_export import export_entity_graph

    t0 = time.perf_counter()
    result = export_entity_graph(
        db_path=effective_db,
        output=args.output,
        fmt=args.format,
        scope=args.scope,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    payload = asdict(result)
    if args.json_output:
        print(_json_envelope(payload, query_ms=elapsed_ms))
    else:
        print(
            f"entity graph exported: {result.output}\n"
            f"  format: {result.format}\n"
            f"  nodes:  {result.nodes}\n"
            f"  edges:  {result.edges}"
        )
    return 0


def _handle_resolve_conflicts(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.govern.conflict_resolver import resolve_conflicts

    t0 = time.perf_counter()
    result = resolve_conflicts(service, dry_run=args.dry_run, limit=args.limit)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(asdict(result), query_ms=elapsed_ms))
    else:
        if args.dry_run:
            print("[DRY RUN] No transitions applied.")
        print(f"conflicts detected={result.pairs_detected} resolved={result.pairs_resolved} skipped={result.pairs_skipped}")
        for res in result.resolutions:
            status = "APPLIED" if res.get("applied") else "SKIPPED"
            skip = f" ({res['skip_reason']})" if res.get("skip_reason") else ""
            print(f"  [{status}] winner={res['winner_id']} loser={res['loser_id']} reason={res['reason']}{skip}")
    return 0


def _handle_check_staleness(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.govern.jobs.staleness import run as run_staleness

    t0 = time.perf_counter()
    result = run_staleness(service.store, Path(args.workspace).resolve(), mode=args.mode, dry_run=args.dry_run, limit=args.limit)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(asdict(result), query_ms=elapsed_ms))
    else:
        if args.dry_run:
            print("[DRY RUN] No transitions applied.")
        print(f"staleness scanned={result.scanned} stale={result.stale_detected} "
              f"already_stale={result.already_stale} skipped_pinned={result.skipped_pinned} "
              f"skipped_no_citations={result.skipped_no_citations}")
        for d in result.details:
            print(f"  [{'APPLIED' if d.get('applied') else 'DETECTED'}] claim={d['claim_id']} "
                  f"files={', '.join(os.path.basename(f) for f in d.get('changed_files', [])[:3])}")
    return 0


def _handle_migrate(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    """v3.20.0-S1: apply pending schema migrations, or report status.

    Default (no flags): apply every pending migration in version order.
    --list: dump known migrations (version + description) without touching the DB.
    --status: query the DB and show applied vs pending per migration.
    """
    from memorymaster.migrations import (
        MigrationRunner,
        discover_migrations,
    )
    from memorymaster.store_factory import is_postgres_dsn

    # --list works without a DB connection at all.
    if getattr(args, "list", False):
        migrations = discover_migrations()
        if args.json_output:
            payload = [{"version": m.version, "description": m.description} for m in migrations]
            print(_json_envelope(payload))
        else:
            print(f"known migrations ({len(migrations)}):")
            for m in migrations:
                print(f"  v{m.version:04d}  {m.description}")
        return 0

    backend = "postgres" if is_postgres_dsn(effective_db) else "sqlite"
    store = service.store
    with store.connect() as conn:
        runner = MigrationRunner(conn, backend=backend)

        if getattr(args, "status", False):
            entries = runner.status()
            if args.json_output:
                payload = [
                    {
                        "version": e.version,
                        "description": e.description,
                        "applied": e.applied,
                        "applied_at": e.applied_at,
                    }
                    for e in entries
                ]
                print(_json_envelope(payload))
            else:
                print(f"backend={backend} db={effective_db}")
                for e in entries:
                    marker = "[applied]" if e.applied else "[pending]"
                    when = f" applied_at={e.applied_at}" if e.applied_at else ""
                    print(f"  v{e.version:04d}  {marker} {e.description}{when}")
            return 0

        # Default: apply pending
        newly = runner.apply_pending()
        if args.json_output:
            print(_json_envelope({"applied": newly, "backend": backend}))
        else:
            if not newly:
                print(f"migrate: nothing to apply (backend={backend}, db={effective_db})")
            else:
                print(f"migrate: applied {len(newly)} migration(s) on backend={backend}:")
                for v in newly:
                    print(f"  v{v:04d}")
    return 0


def _handle_export_delta(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    """Export claims changed since a watermark into a small SQLite delta file.

    The delta file is a valid `merge-db --source` input. Prints (or JSON-emits)
    the export counts and the new watermark — callers should record
    `max_updated_at` and pass it as `--since` on the next run.
    """
    from memorymaster.bridges.delta_sync import export_delta

    t0 = time.perf_counter()
    result = export_delta(effective_db, args.since, args.output)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(result, query_ms=elapsed_ms))
    else:
        since_label = result["since"] or "(full export)"
        print(
            f"export-delta: {result['exported']} claims + {result['citations']} citations "
            f"since {since_label} -> {args.output}"
        )
        if result["max_updated_at"]:
            print(f"  next watermark (--since): {result['max_updated_at']}")
        else:
            print("  delta is empty — nothing changed since the watermark")
    return 0


