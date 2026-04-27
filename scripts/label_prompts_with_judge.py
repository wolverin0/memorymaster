"""LLM-judge: label which retrieved claims actually answer each synthetic prompt.

For each prompt in the input JSONL:
  1. Run the production recall hook to get the top-K (default 20) candidate claims.
  2. Send (prompt + candidate snippets) to a haiku judge.
  3. Judge returns the subset of claim IDs that genuinely answer the prompt.
  4. Write {sha1_16(prompt): [claim_ids]} into the labels JSON.

Usage:
    python scripts/label_prompts_with_judge.py \
        --prompts artifacts/real-prompts-1000.jsonl \
        --db memorymaster.db \
        --labels-out artifacts/real-prompts-1000-labels.json \
        --top-k 20 \
        --max-prompts 1000

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


def _sha1_16(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _judge_prompt(prompt: str, candidates: list[dict]) -> str:
    candidate_lines = "\n".join(
        f"[{c['id']}] {c['text'][:300]}" for c in candidates
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
    # Use the same return_ids=True path as the eval harness.
    from memorymaster import context_hook

    # Recall returns rendered bullet text; we need ids + raw claim text.
    # Easiest: get the IDs from recall, then fetch claim text from DB.
    try:
        # context_hook.recall signature:
        #   recall(query, *, db_path='', budget=2000, format='text', skip_qdrant=False, return_ids=False)
        result = context_hook.recall(
            prompt,
            db_path=db_path,
            return_ids=True,
        )
        if isinstance(result, tuple):
            _, ids = result
        else:
            ids = []
    except Exception as exc:
        print(f"[label] recall() raised: {exc}", flush=True)
        ids = []

    if not ids:
        return []

    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        rows = []
        for cid in ids[:top_k]:
            row = conn.execute(
                "SELECT id, text FROM claims WHERE id = ?", (cid,)
            ).fetchone()
            if row:
                rows.append({"id": row[0], "text": row[1] or ""})
        return rows
    finally:
        conn.close()


def _call_judge(prompt: str, candidates: list[dict]) -> list[int]:
    """Single LLM call to the judge. Returns list of claim IDs."""
    from memorymaster.llm_provider import call_llm, parse_json_response

    judge_text = _judge_prompt(prompt, candidates)
    raw = call_llm(judge_text, "")
    if not raw:
        return []

    parsed = parse_json_response(raw)
    # parse_json_response returns list of dicts; we want bare ints.
    # If it returns [{"id": 123}, ...] coerce; otherwise try raw int parsing.
    ids: list[int] = []
    for item in parsed:
        if isinstance(item, int):
            ids.append(item)
        elif isinstance(item, dict):
            v = item.get("id") or item.get("claim_id")
            if isinstance(v, int):
                ids.append(v)
        elif isinstance(item, str) and item.strip().lstrip("-").isdigit():
            ids.append(int(item.strip()))

    # Fallback: regex-extract integers from raw if parser missed it
    if not ids:
        import re

        ids = [int(m) for m in re.findall(r"\b\d{2,8}\b", raw)]
    return ids


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prompts", type=Path, required=True)
    p.add_argument("--db", type=str, required=True)
    p.add_argument("--labels-out", type=Path, required=True)
    p.add_argument("--top-k", type=int, default=20)
    p.add_argument("--max-prompts", type=int, default=1000)
    p.add_argument(
        "--checkpoint-every",
        type=int,
        default=25,
        help="Flush labels JSON every N prompts (resume-safe).",
    )
    args = p.parse_args()

    # Force claude_cli for the judge — Gemini API is rate-limited and slow.
    # Direct assignment (NOT setdefault) — avoid the v3.5.0 hook bug where
    # an inherited shell env left the provider stale.
    os.environ["MEMORYMASTER_LLM_PROVIDER"] = "claude_cli"
    os.environ["MEMORYMASTER_LLM_MODEL"] = "claude-haiku-4-5-20251001"

    prompts: list[dict] = []
    with args.prompts.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            prompts.append(json.loads(line))
    prompts = prompts[: args.max_prompts]

    # Resume from existing labels file if present
    labels: dict[str, list[int]] = {}
    if args.labels_out.exists():
        labels = json.loads(args.labels_out.read_text(encoding="utf-8")).get(
            "labels", {}
        )
        print(f"[label] resuming from {len(labels)} existing labels", flush=True)

    t_start = time.monotonic()
    for i, p_obj in enumerate(prompts, 1):
        text = p_obj["text"]
        sha = _sha1_16(text)
        if sha in labels:
            continue
        try:
            cands = _get_candidates(args.db, text, args.top_k)
            if not cands:
                labels[sha] = []
            else:
                ids = _call_judge(text, cands)
                # Filter to only IDs that were actually in the candidate set
                cand_ids = {c["id"] for c in cands}
                labels[sha] = [i for i in ids if i in cand_ids][:5]
        except Exception as exc:
            print(f"[label] {i}: ERROR {exc}", flush=True)
            labels[sha] = []

        if i % 5 == 0:
            elapsed = time.monotonic() - t_start
            avg = elapsed / i
            eta = avg * (len(prompts) - i)
            print(
                f"[label] {i}/{len(prompts)}  avg={avg:.1f}s  eta={eta/60:.1f}min  "
                f"last={labels[sha]}",
                flush=True,
            )

        if i % args.checkpoint_every == 0:
            args.labels_out.write_text(
                json.dumps({"labels": labels}, indent=2), encoding="utf-8"
            )

    args.labels_out.write_text(
        json.dumps({"labels": labels}, indent=2), encoding="utf-8"
    )
    n_labeled = sum(1 for v in labels.values() if v)
    print(
        f"[label] DONE wrote {len(labels)} labels "
        f"({n_labeled} non-empty) to {args.labels_out}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
