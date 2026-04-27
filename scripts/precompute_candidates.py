"""Pre-compute top-K recall candidates for each prompt + write a chunked batch file
suitable for parallel labeling by subagents.

For each prompt in the input JSONL, run production recall once and capture the
top-K candidate (id, text snippet) pairs. Output is a JSON file with one entry
per prompt, organized as one chunk per output file so multiple labeling
subagents can work in parallel.

Usage:
    python scripts/precompute_candidates.py \\
        --prompts artifacts/real-prompts-1000.jsonl \\
        --db memorymaster.db \\
        --out-dir artifacts/label-batches \\
        --chunk-size 100 --top-k 15
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path


def _sha1_16(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _get_candidates(conn: sqlite3.Connection, prompt: str, top_k: int) -> list[dict]:
    from memorymaster import context_hook

    try:
        result = context_hook.recall(prompt, db_path=conn.execute("PRAGMA database_list").fetchone()[2], return_ids=True)
        if isinstance(result, tuple):
            _, ids = result
        else:
            ids = []
    except Exception as exc:
        print(f"[recall] error: {exc}", flush=True)
        ids = []

    rows = []
    for cid in ids[:top_k]:
        row = conn.execute("SELECT id, text FROM claims WHERE id = ?", (cid,)).fetchone()
        if row:
            rows.append({"id": row[0], "text": (row[1] or "")[:300]})
    return rows


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prompts", type=Path, required=True)
    p.add_argument("--db", type=str, required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--chunk-size", type=int, default=100)
    p.add_argument("--top-k", type=int, default=15)
    args = p.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    prompts: list[dict] = []
    with args.prompts.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            prompts.append(json.loads(line))

    conn = sqlite3.connect(args.db)
    t0 = time.monotonic()
    items: list[dict] = []
    for i, p_obj in enumerate(prompts, 1):
        text = p_obj["text"]
        sha = _sha1_16(text)
        cands = _get_candidates(conn, text, args.top_k)
        items.append({"sha": sha, "prompt": text, "candidates": cands})
        if i % 50 == 0:
            print(f"[precompute] {i}/{len(prompts)}  wall={time.monotonic()-t0:.1f}s", flush=True)
    conn.close()

    # Write chunks
    n_chunks = (len(items) + args.chunk_size - 1) // args.chunk_size
    for ci in range(n_chunks):
        chunk = items[ci * args.chunk_size : (ci + 1) * args.chunk_size]
        out = args.out_dir / f"in-chunk{ci+1:02d}.json"
        out.write_text(json.dumps(chunk, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[precompute] DONE wrote {n_chunks} chunks ({len(items)} prompts) to {args.out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
