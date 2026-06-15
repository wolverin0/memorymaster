"""Evaluate MemPalace-style verbatim recall vs baseline on 30-prompt set.

Runs the full ``context_hook.recall()`` pipeline — which means the
MEMORYMASTER_RECALL_VERBATIM / W_VERBATIM env vars are honoured end-to-end.

Compares three conditions:

1. BASELINE     — verbatim stream OFF (shipped default).
2. VERBATIM+0.0 — stream ON but weight 0.0 (candidate pool enlarged, ranking
                  untouched). Isolates the "does it add zero-hit rescues?"
                  question from the "does it rerank?" question.
3. VERBATIM+Wv  — stream ON with user-supplied weight (default 0.2).

Outputs per-prompt delta + aggregate metrics. Read-only against the DB —
calls ``recall()`` which internally opens the store, but no claim is
ingested or mutated.

Usage:
    python scripts/eval_verbatim_recall.py [--weight 0.2] [--verbose]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from memorymaster.recall.context_hook import recall  # noqa: E402
from memorymaster.recall.recall_tokenizer import _candidate_tokens  # noqa: E402


def _load_prompts(path: Path) -> list[str]:
    out: list[str] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            text = (rec.get("text") or "").strip()
            if text:
                out.append(text)
    return out


def _parse_output(raw: str) -> list[str]:
    """Return the list of bullet-lines in a recall() output."""
    return [ln for ln in raw.splitlines() if ln.startswith("- ")]


def _precision_at_k(lines: list[str], prompt: str, k: int = 5,
                    min_overlap: int = 2) -> tuple[float, int]:
    """Return (precision@k, hit count) using the same proxy label as
    scripts/eval_recall_precision_at_5.py."""
    ptoks = {t for t in _candidate_tokens(prompt) if len(t) >= 3}
    head = lines[:k]
    if not head:
        return 0.0, 0
    hits = 0
    for ln in head:
        # Strip the "- " prefix + optional wiki tag.
        body = ln[2:].split("  (compiled in")[0]
        btoks = {t for t in _candidate_tokens(body) if len(t) >= 3}
        if len(btoks & ptoks) >= min_overlap:
            hits += 1
    return hits / len(head), hits


def _run_condition(prompts: list[str], db_path: str,
                   verbatim_on: bool, weight: float,
                   verbose: bool = False) -> dict:
    """Execute recall() across all prompts under the given env conditions.

    Returns {non_empty: n, p5_mean: float, per_prompt: [{...}]}.
    """
    if verbatim_on:
        os.environ["MEMORYMASTER_RECALL_VERBATIM"] = "1"
        os.environ["MEMORYMASTER_RECALL_W_VERBATIM"] = str(weight)
    else:
        os.environ.pop("MEMORYMASTER_RECALL_VERBATIM", None)
        os.environ.pop("MEMORYMASTER_RECALL_W_VERBATIM", None)

    non_empty = 0
    p5s: list[float] = []
    per_prompt: list[dict] = []
    for i, prompt in enumerate(prompts, 1):
        out = recall(prompt, db_path=db_path, skip_qdrant=True)
        lines = _parse_output(out)
        if lines:
            non_empty += 1
        p5, hits = _precision_at_k(lines, prompt, k=5, min_overlap=2)
        p5s.append(p5)
        per_prompt.append({
            "idx": i,
            "non_empty": bool(lines),
            "p5": p5,
            "hits_top5": hits,
            "n_lines": len(lines),
            "prompt_head": prompt[:70],
        })
        if verbose:
            print(f"  #{i:>2} non_empty={bool(lines)}  p@5={p5:.2f}  "
                  f"lines={len(lines)}  {prompt[:60]!r}")

    return {
        "non_empty": non_empty,
        "p5_mean": sum(p5s) / max(1, len(p5s)),
        "per_prompt": per_prompt,
    }


def _diff_per_prompt(base: list[dict], cand: list[dict]) -> list[dict]:
    """Per-prompt before/after deltas. Same index order assumed."""
    assert len(base) == len(cand)
    out = []
    for b, c in zip(base, cand):
        out.append({
            "idx": b["idx"],
            "prompt": b["prompt_head"],
            "non_empty_delta": int(c["non_empty"]) - int(b["non_empty"]),
            "p5_delta": c["p5"] - b["p5"],
            "hits_delta": c["hits_top5"] - b["hits_top5"],
            "lines_delta": c["n_lines"] - b["n_lines"],
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prompts", default="artifacts/real-prompts.jsonl")
    ap.add_argument("--db", default="memorymaster.db")
    ap.add_argument("--weight", type=float, default=0.2,
                    help="W_VERBATIM to test (default 0.2)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    prompts_path = Path(args.prompts)
    if not prompts_path.is_absolute():
        prompts_path = REPO / prompts_path
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = REPO / db_path

    if not prompts_path.exists() or not db_path.exists():
        missing = prompts_path if not prompts_path.exists() else db_path
        print(f"ERROR: missing {missing}")
        return 2

    prompts = _load_prompts(prompts_path)
    print(f"Loading {len(prompts)} prompts from {prompts_path.name}")
    print(f"DB: {db_path.name}")

    print("\n[1/3] BASELINE (verbatim OFF)")
    baseline = _run_condition(prompts, str(db_path), verbatim_on=False,
                              weight=0.0, verbose=args.verbose)
    print(f"  non-empty: {baseline['non_empty']}/{len(prompts)}")
    print(f"  mean p@5 : {baseline['p5_mean']:.4f}")

    print("\n[2/3] VERBATIM ON, W_VERBATIM=0.0 (pool widening only)")
    zero_w = _run_condition(prompts, str(db_path), verbatim_on=True,
                            weight=0.0, verbose=args.verbose)
    print(f"  non-empty: {zero_w['non_empty']}/{len(prompts)}  "
          f"(delta {zero_w['non_empty'] - baseline['non_empty']:+d})")
    print(f"  mean p@5 : {zero_w['p5_mean']:.4f}  "
          f"(delta {zero_w['p5_mean'] - baseline['p5_mean']:+.4f})")

    print(f"\n[3/3] VERBATIM ON, W_VERBATIM={args.weight}")
    active = _run_condition(prompts, str(db_path), verbatim_on=True,
                            weight=args.weight, verbose=args.verbose)
    print(f"  non-empty: {active['non_empty']}/{len(prompts)}  "
          f"(delta {active['non_empty'] - baseline['non_empty']:+d})")
    print(f"  mean p@5 : {active['p5_mean']:.4f}  "
          f"(delta {active['p5_mean'] - baseline['p5_mean']:+.4f})")

    # Surface per-prompt wins/losses.
    diffs_w = _diff_per_prompt(baseline["per_prompt"], active["per_prompt"])
    rescued = [d for d in diffs_w if d["non_empty_delta"] > 0]
    lost = [d for d in diffs_w if d["non_empty_delta"] < 0]
    up = [d for d in diffs_w if d["p5_delta"] > 0]
    down = [d for d in diffs_w if d["p5_delta"] < 0]

    print("\nPer-prompt non-empty rescues (zero-hit -> non-empty):")
    if rescued:
        for d in rescued:
            print(f"  #{d['idx']}: {d['prompt']!r}")
    else:
        print("  (none)")

    print("\nPer-prompt non-empty regressions (non-empty -> zero):")
    if lost:
        for d in lost:
            print(f"  #{d['idx']}: {d['prompt']!r}")
    else:
        print("  (none)")

    print(f"\np@5 movers (|delta| > 0): +{len(up)} / -{len(down)}")
    for d in sorted(diffs_w, key=lambda x: x["p5_delta"], reverse=True)[:5]:
        if d["p5_delta"] > 0:
            print(f"  +#{d['idx']} ({d['p5_delta']:+.2f})  {d['prompt']!r}")
    for d in sorted(diffs_w, key=lambda x: x["p5_delta"])[:5]:
        if d["p5_delta"] < 0:
            print(f"  -#{d['idx']} ({d['p5_delta']:+.2f})  {d['prompt']!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
