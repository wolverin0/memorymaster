"""Offline judge labels for recall prompts with resumable provenance.

Key terms: recall evaluation, OpenCode OAuth, judge provenance, fixture hash.
Read this when generating ground-truth labels for retrieval evaluation.
This script never participates in capture, recall, or production claim writes.
Provider failures remain explicit errors and never become empty labels.
Existing label files resume without rejudging completed prompts.

Usage:
    python scripts/label_prompts_with_judge.py \\
        --prompts artifacts/real-prompts-1000.jsonl \\
        --db memorymaster.db \\
        --labels-out artifacts/real-prompts-1000-labels.json \\
        --judge-provider opencode

The output is consumed by scripts/eval_recall_precision_at_5.py via the
``<prompts>-labels.json`` convention.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from memorymaster.evaluation.opencode_judge import OpenCodeJudge, OpenCodeJudgeError


def _sha1_16(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _judge_prompt(prompt: str, candidates: list[dict]) -> str:
    candidate_lines = "\n".join(
        f"[{candidate['id']}] {candidate['text'][:300]}" for candidate in candidates
    )
    return f"""You are a relevance judge. Given a USER QUERY and a list of CANDIDATE memory claims, return the subset of claim IDs that genuinely answer the query.

USER QUERY: {prompt}

CANDIDATES (id and snippet):
{candidate_lines}

Rules:
- Return ONLY claim IDs that DIRECTLY answer the query (not tangentially related).
- An empty list is a valid answer if no candidate genuinely answers.
- Return JSON ARRAY ONLY of integer IDs, no prose, no fence. Example: [123, 456]
- Be strict — pick at most 5, prefer 0-3 high-quality matches over many weak ones."""


def _get_candidates(db_path: str, prompt: str, top_k: int) -> list[dict]:
    """Run production recall via context_hook and return top-K candidates."""
    from memorymaster.recall import context_hook

    try:
        result = context_hook.recall(prompt, db_path=db_path, return_ids=True)
        ids = result[1] if isinstance(result, tuple) else []
    except Exception as exc:
        print(f"[label] recall() raised: {exc}", flush=True)
        ids = []
    if not ids:
        return []

    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        rows = []
        for claim_id in ids[:top_k]:
            row = conn.execute(
                "SELECT id, text FROM claims WHERE id = ?", (claim_id,)
            ).fetchone()
            if row:
                rows.append({"id": row[0], "text": row[1] or ""})
        return rows
    finally:
        conn.close()


def _parse_judge_ids(raw: str) -> list[int]:
    from memorymaster.core.llm_provider import parse_json_response

    parsed = parse_json_response(raw)
    ids: list[int] = []
    for item in parsed:
        if isinstance(item, int):
            ids.append(item)
        elif isinstance(item, dict):
            value = item.get("id") or item.get("claim_id")
            if isinstance(value, int):
                ids.append(value)
        elif isinstance(item, str) and item.strip().lstrip("-").isdigit():
            ids.append(int(item.strip()))
    if not ids:
        import re

        ids = [int(match) for match in re.findall(r"\b\d{2,8}\b", raw)]
    return ids


def _call_judge(
    prompt: str,
    candidates: list[dict],
    *,
    provider: str = "claude_cli",
    judge: OpenCodeJudge | None = None,
) -> tuple[list[int], dict]:
    """Run one judge call and return IDs plus content-free provenance."""
    judge_text = _judge_prompt(prompt, candidates)
    if provider == "opencode":
        if judge is None:
            raise OpenCodeJudgeError("judge_unavailable", "OpenCode judge is unavailable.")
        result = judge.complete(judge_text)
        return _parse_judge_ids(result.text), result.provenance()

    from memorymaster.core.llm_provider import call_llm

    started = time.monotonic()
    raw = call_llm(judge_text, "")
    if not raw:
        raise RuntimeError("Judge returned no output.")
    provenance = {
        "provider": provider,
        "model": os.environ.get("MEMORYMASTER_LLM_MODEL", ""),
        "prompt_hash": hashlib.sha256(judge_text.encode("utf-8")).hexdigest(),
        "latency_ms": int((time.monotonic() - started) * 1000),
    }
    return _parse_judge_ids(raw), provenance


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompts", type=Path, required=True)
    parser.add_argument("--db", type=str, required=True)
    parser.add_argument("--labels-out", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--max-prompts", type=int, default=1000)
    parser.add_argument(
        "--judge-provider", choices=("claude_cli", "opencode"), default="claude_cli"
    )
    parser.add_argument("--judge-model", default="openai/gpt-5.4-mini")
    parser.add_argument("--judge-effort", default="medium")
    parser.add_argument("--judge-timeout", type=int, default=180)
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=25,
        help="Flush labels JSON every N prompts (resume-safe).",
    )
    return parser.parse_args()


def _load_prompts(path: Path, maximum: int) -> list[dict]:
    prompts: list[dict] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                prompts.append(json.loads(line))
    return prompts[:maximum]


def _load_resume(path: Path) -> tuple[dict, dict, dict]:
    if not path.exists():
        return {}, {}, {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return (
        dict(payload.get("labels", {})),
        dict(payload.get("provenance", {})),
        dict(payload.get("errors", {})),
    )


def _judge_config(args: argparse.Namespace) -> dict:
    model = (
        args.judge_model
        if args.judge_provider == "opencode"
        else os.environ.get("MEMORYMASTER_LLM_MODEL", "")
    )
    return {
        "provider": args.judge_provider,
        "model": model,
        "effort": args.judge_effort if args.judge_provider == "opencode" else "",
        "timeout_seconds": args.judge_timeout,
    }


def _output_payload(
    args: argparse.Namespace, labels: dict, provenance: dict, errors: dict
) -> dict:
    return {
        "schema": "memorymaster.recall-labels.v2",
        "labels": labels,
        "judge": _judge_config(args),
        "fixture": {
            "prompts_sha256": hashlib.sha256(args.prompts.read_bytes()).hexdigest(),
            "prompt_count": len(labels) + len(errors),
        },
        "provenance": provenance,
        "errors": errors,
    }


def _write_output(
    args: argparse.Namespace, labels: dict, provenance: dict, errors: dict
) -> None:
    args.labels_out.parent.mkdir(parents=True, exist_ok=True)
    args.labels_out.write_text(
        json.dumps(_output_payload(args, labels, provenance, errors), indent=2),
        encoding="utf-8",
    )


def _judge_client(args: argparse.Namespace) -> OpenCodeJudge | None:
    if args.judge_provider != "opencode":
        return None
    return OpenCodeJudge(
        model=args.judge_model,
        effort=args.judge_effort,
        timeout=args.judge_timeout,
    )


def _configure_legacy_provider(args: argparse.Namespace) -> None:
    if args.judge_provider != "claude_cli":
        return
    os.environ["MEMORYMASTER_LLM_PROVIDER"] = "claude_cli"
    os.environ["MEMORYMASTER_LLM_MODEL"] = "claude-haiku-4-5-20251001"


def _error_record(exc: Exception) -> dict[str, str]:
    return {
        "code": getattr(exc, "code", exc.__class__.__name__.lower()),
        "detail": str(exc)[:500],
    }


def _label_one(
    args: argparse.Namespace,
    judge: OpenCodeJudge | None,
    text: str,
) -> tuple[list[int], dict]:
    candidates = _get_candidates(args.db, text, args.top_k)
    if not candidates:
        return [], {"status": "skipped", "reason": "no_candidates"}
    ids, provenance = _call_judge(
        text, candidates, provider=args.judge_provider, judge=judge
    )
    candidate_ids = {candidate["id"] for candidate in candidates}
    return [claim_id for claim_id in ids if claim_id in candidate_ids][:5], provenance


def _progress(index: int, total: int, started: float, label: object) -> None:
    elapsed = time.monotonic() - started
    average = elapsed / index
    eta = average * (total - index)
    print(
        f"[label] {index}/{total} avg={average:.1f}s "
        f"eta={eta / 60:.1f}min last={label}",
        flush=True,
    )


def main() -> int:
    args = _parse_args()
    _configure_legacy_provider(args)
    prompts = _load_prompts(args.prompts, args.max_prompts)
    labels, provenance, errors = _load_resume(args.labels_out)
    if labels:
        print(f"[label] resuming from {len(labels)} existing labels", flush=True)
    judge = _judge_client(args)
    started = time.monotonic()
    for index, prompt_object in enumerate(prompts, 1):
        text = prompt_object["text"]
        sha = _sha1_16(text)
        if sha in labels:
            continue
        try:
            labels[sha], provenance[sha] = _label_one(args, judge, text)
            errors.pop(sha, None)
        except Exception as exc:
            errors[sha] = _error_record(exc)
            print(f"[label] {index}: ERROR {exc}", flush=True)
        if index % 5 == 0:
            _progress(index, len(prompts), started, labels.get(sha, "ERROR"))
        if index % args.checkpoint_every == 0:
            _write_output(args, labels, provenance, errors)
    _write_output(args, labels, provenance, errors)
    non_empty = sum(1 for value in labels.values() if value)
    print(
        f"[label] DONE wrote {len(labels)} labels "
        f"({non_empty} non-empty) to {args.labels_out}",
        flush=True,
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
