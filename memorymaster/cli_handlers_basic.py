"""CLI handlers — basic operations (claims, query, lifecycle, ops).

Half of the handlers live here; curation/wiki/dream handlers are in
`cli_handlers_curation.py`. Both modules contribute entries to the
COMMAND_HANDLERS dispatch table defined in `cli_handlers_curation.py`.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path
import time

from memorymaster.cli_helpers import (
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
from memorymaster.scheduler import run_daemon
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
    from memorymaster.qdrant_backend import QdrantBackend

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
    from memorymaster.metrics_exporter import export_metrics

    snapshot = export_metrics(events_jsonl=[Path(p) for p in args.events_jsonl],
        out_prom=Path(args.out_prom), out_json=Path(args.out_json))
    c = snapshot.get("counters", {})
    print(json.dumps({"command": "export-metrics",
        "events_total": int(c.get("events_total", 0)), "transitions_total": int(c.get("transitions_total", 0)),
        "status_total": int(c.get("status_total", 0)),
        "out_prom": args.out_prom, "out_json": args.out_json}, indent=2))
    return 0


def _handle_init_db(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    t0 = time.perf_counter()
    service.init_db()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    db_path = effective_db if "://" in effective_db else str(Path(effective_db).resolve())
    if args.json_output:
        print(_json_envelope({"db": db_path, "stealth": _stealth_active(args)}, query_ms=elapsed_ms))
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
        from memorymaster.dream_bridge import dream_sync
        try:
            sync_result = dream_sync(effective_db, project_path=args.dream_project)
            print(f"\ndream-sync: ingested={sync_result.get('ingested', 0)} "
                  f"seeded={sync_result.get('seeded', 0)} "
                  f"skipped={sync_result.get('skipped', 0)}")
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"\ndream-sync skipped: {exc}")

    return 0


def _handle_query(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    resolve_allow_sensitive_access(allow_sensitive=args.allow_sensitive, context="cli.query")
    if getattr(args, "as_of", ""):
        t0 = time.perf_counter()
        claims = service.store.query_as_of(args.as_of)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if args.json_output:
            print(_json_envelope([_claim_to_dict(c) for c in claims], total=len(claims), query_ms=elapsed_ms))
        else:
            for c in claims:
                print_claim(c)
            print(f"rows={len(claims)}")
        return 0
    if getattr(args, "auto_classify", False):
        from memorymaster.query_classifier import classify_query, recommended_retrieval_mode
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
        print(f"rows={len(rows_data)}")
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
        scope_allowlist=parse_scope_allowlist(args.scope_allowlist),
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
    result = service.compact(retain_days=args.retain_days, event_retain_days=args.event_retain_days)
    print(json.dumps(result, indent=2))
    return 0


def _handle_compact_summaries(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.llm_steward import _parse_api_keys
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
    result = service.dedup(threshold=args.threshold, min_text_overlap=args.min_text_overlap, dry_run=args.dry_run)
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
    from memorymaster.review import build_review_queue, queue_to_dicts

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
    from memorymaster.dashboard import create_dashboard_server

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
        from memorymaster.operator import MemoryOperator, OperatorConfig
    except Exception as exc:
        print(f"error: run-operator unavailable: could not import memorymaster.operator ({exc})")
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
    from memorymaster.steward import run_steward

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
        artifact_path=Path(args.artifact_json),
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if args.json_output:
        print(_json_envelope(json.loads(json.dumps(result, default=_json_default)), query_ms=elapsed_ms))
    else:
        print(json.dumps(result, indent=2, default=_json_default))
    return 0


def _handle_steward_proposals(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.steward import list_steward_proposals

    rows = list_steward_proposals(service, limit=args.limit, include_resolved=args.include_resolved)
    print(json.dumps({"rows": len(rows), "proposals": rows}, indent=2, default=_json_default))
    return 0


def _handle_resolve_proposal(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    if args.proposal_event_id is None and args.claim_id is None:
        raise ValueError("resolve-proposal requires --proposal-event-id or --claim-id")
    from memorymaster.steward import resolve_steward_proposal

    result = resolve_steward_proposal(service, action=args.action,
        proposal_event_id=args.proposal_event_id, claim_id=args.claim_id, apply_on_approve=not args.no_apply)
    print(json.dumps(result, indent=2, default=_json_default))
    return 0


def _handle_ready(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.conflict_resolver import detect_conflicts

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


def _handle_resolve_conflicts(args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str) -> int:
    from memorymaster.conflict_resolver import resolve_conflicts

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
    from memorymaster.jobs.staleness import run as run_staleness

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


