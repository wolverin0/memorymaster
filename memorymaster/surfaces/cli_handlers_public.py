"""CLI rendering for the stable public v1 operations."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from memorymaster.public.v1 import forget, improve, recall, remember
from memorymaster.public.demo import run_disposable_demo


def _emit(payload: object, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def handle_remember(
    args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str
) -> int:
    receipt = remember(
        text=args.text,
        path=args.file,
        source_uri=args.url or args.source_uri,
        scope=args.scope,
        source_agent=args.source_agent,
        db=effective_db,
        workspace=args.workspace,
    )
    if args.json_output:
        _emit(asdict(receipt), json_output=True)
    else:
        evidence = receipt.evidence["id"] if receipt.evidence else "awaiting"
        print(
            f"remembered source_item={receipt.source_item['id']} evidence={evidence} "
            f"jobs={','.join(map(str, receipt.job_ids))} deduplicated={receipt.deduplicated}"
        )
        for warning in receipt.warnings:
            print(f"warning: {warning}")
    return 0


def handle_recall(
    args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str
) -> int:
    scopes = [item.strip() for item in args.scope_allowlist.split(",") if item.strip()]
    receipt = recall(
        args.query,
        scope_allowlist=scopes or None,
        token_budget=args.budget,
        trust_mode=args.trust_mode,
        output_format=args.output_format,
        db=effective_db,
        workspace=args.workspace,
    )
    if args.json_output or args.output_format == "json":
        _emit(asdict(receipt), json_output=True)
    else:
        print(receipt.output)
    return 0


def handle_forget(
    args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str
) -> int:
    receipt = forget(
        claim_id=args.claim_id,
        source_item_id=args.source_item_id,
        apply=args.apply,
        db=effective_db,
        workspace=args.workspace,
    )
    if args.json_output:
        _emit(asdict(receipt), json_output=True)
    else:
        mode = "applied" if receipt.apply else "preview"
        print(f"forget {mode}: {len(receipt.actions)} action(s); evidence preserved")
        for action in receipt.actions:
            print(f"  {json.dumps(action, sort_keys=True)}")
    return 0


def handle_improve(
    args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str
) -> int:
    receipt = improve(
        scope=args.scope,
        max_items=args.max_items,
        db=effective_db,
        workspace=args.workspace,
    )
    if args.json_output:
        _emit(asdict(receipt), json_output=True)
    else:
        print(
            f"improve queued: claims={receipt.queued['extract_claims']} "
            f"graph={receipt.queued['extract_graph']} "
            f"steward_review_due={receipt.steward_review_due}"
        )
    return 0


def handle_demo(
    args: argparse.Namespace, service, parser: argparse.ArgumentParser, effective_db: str
) -> int:
    report = run_disposable_demo()
    if args.json_output:
        _emit(report, json_output=True)
    else:
        print(
            f"demo complete: captures={report['captures']} "
            f"jobs={report['fixture_jobs_completed']} "
            f"promoted={report['promoted_claim_id']}"
        )
        print(
            f"recall={','.join(map(str, report['recall_claim_ids']))} "
            f"citations={len(report['recall_citations'])} "
            f"graph_paths={len(report['graph_paths'])}"
        )
        print("temporary database disposed")
    return 0
