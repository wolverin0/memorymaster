"""Evaluate recall-hook hit-rate on held-out real prompts.

Runs OLD path (raw prompt -> FTS5, AND-joined) and NEW path
(extract_query_tokens -> per-token fanout, union hits) against the 30
prompts in artifacts/real-prompts.jsonl. Read-only on the DB.

Usage: python scripts/eval_recall_quality.py [--verbose]
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from memorymaster.recall.recall_tokenizer import extract_query_tokens  # noqa: E402
from memorymaster.service import MemoryService  # noqa: E402


def _load_prompts(path: Path) -> list[str]:
    out: list[str] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rec = json.loads(line)
                text = (rec.get("text") or "").strip()
                if text:
                    out.append(text)
    return out


def _run_raw(svc: MemoryService, q: str) -> int:
    rows = svc.query_rows(
        query_text=q, limit=5, retrieval_mode="legacy",
        include_candidates=True, scope_allowlist=None,
    )
    return len(rows)


def _run_tokenized(svc: MemoryService, tokens: str, limit: int = 8,
                   raw_prompt: str | None = None,
                   db_path: str | None = None) -> int:
    if not tokens and not raw_prompt:
        return 0
    token_list = tokens.split() if tokens else []
    seen: set[int] = set()
    per_token = max(3, limit // max(1, len(token_list) or 1))
    hits = 0
    for tok in token_list:
        rows = svc.query_rows(
            query_text=tok, limit=per_token, retrieval_mode="legacy",
            include_candidates=True, scope_allowlist=None,
        )
        for row in rows:
            cid = getattr(row.get("claim"), "id", None)
            if cid is None or cid in seen:
                continue
            seen.add(cid)
            hits += 1
            if hits >= limit:
                return hits
    # Optional verbatim stream — gated on MEMORYMASTER_RECALL_VERBATIM=1.
    # Acts as a rescue stream when the claims pipeline returned nothing.
    if raw_prompt and db_path and hits < limit:
        try:
            from memorymaster.recall.verbatim_recall import (
                is_enabled as _verbatim_enabled,
                recall_verbatim,
            )
            if _verbatim_enabled():
                verbatim_hits = recall_verbatim(
                    raw_prompt, scope=None, db_path=db_path,
                    limit=limit - hits,
                )
                hits += len(verbatim_hits)
        except Exception:
            pass
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prompts", default="artifacts/real-prompts.jsonl")
    ap.add_argument("--db", default="memorymaster.db")
    ap.add_argument("--max-tokens", type=int, default=6)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    prompts_path = Path(args.prompts)
    if not prompts_path.is_absolute():
        prompts_path = REPO / prompts_path
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = REPO / db_path

    if not prompts_path.exists() or not db_path.exists():
        print(f"ERROR: missing {prompts_path if not prompts_path.exists() else db_path}")
        return 2

    prompts = _load_prompts(prompts_path)
    svc = MemoryService(db_target=str(db_path), workspace_root=REPO)

    before, after = 0, 0
    for i, prompt in enumerate(prompts, 1):
        old_n = _run_raw(svc, prompt)
        tokens = extract_query_tokens(prompt, str(db_path), max_tokens=args.max_tokens)
        new_n = _run_tokenized(svc, tokens, raw_prompt=prompt,
                                db_path=str(db_path))
        before += 1 if old_n > 0 else 0
        after += 1 if new_n > 0 else 0
        if args.verbose:
            flag = "+" if new_n > old_n else (" " if new_n == old_n else "-")
            print(f"{flag} #{i:>2}  before={old_n} after={new_n}  "
                  f"tokens={tokens!r}  prompt={prompt[:70]!r}")

    total = len(prompts)
    print(f"BEFORE hit-rate: {before}/{total}  ({100 * before / total:.1f}%)")
    print(f"AFTER  hit-rate: {after}/{total}  ({100 * after / total:.1f}%)")
    print(f"Target: >=70% after.  {'PASS' if after / total >= 0.70 else 'FAIL'}")
    return 0 if after / total >= 0.70 else 1


if __name__ == "__main__":
    raise SystemExit(main())
