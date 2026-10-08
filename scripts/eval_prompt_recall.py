"""Quality regression of the prompt-recall hook against a fixed, labeled prompt set.

Runs the real ``context_hook.recall`` pipeline (with each prompt's session cwd)
over the 200 labeled prompts of 2026-10-06 and reports nDCG@10, useful claims
and noise per prompt, and how many injected claims carry no label. Read-only:
access records, latency lines and the Jev surface are disabled in this process.

The set holds real operator prompts, so it lives outside the repository:
~/.memorymaster/evals/prompt-recall-20261006/{queries.json,labels.jsonl}.
New claims have no label and count as not useful; when ``unlabeled_share``
grows, label the new pairs before trusting a drop.

    python scripts/eval_prompt_recall.py --mode dense
    python scripts/eval_prompt_recall.py --mode lexical
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path

DEFAULT_SET = Path.home() / ".memorymaster" / "evals" / "prompt-recall-20261006"
DEFAULT_DB = Path(__file__).resolve().parents[1] / "memorymaster.db"


def _ndcg(gains: list[int], ideal: list[int]) -> float | None:
    def dcg(values: list[int]) -> float:
        return sum((2 ** v - 1) / math.log2(k + 2) for k, v in enumerate(values[:10]))

    best = dcg(sorted(ideal, reverse=True))
    return dcg(gains) / best if best else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("dense", "lexical"), default="dense")
    parser.add_argument("--set", default=str(DEFAULT_SET))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=None, help="also append the summary as one JSON line here")
    parser.add_argument("--unlabeled-out", default=None,
                        help="write the injected pairs that carry no label, ready for a judge, to this JSON file")
    args = parser.parse_args(argv)

    os.environ["MEMORYMASTER_RECALL_DENSE"] = "1" if args.mode == "dense" else "0"
    os.environ["MEMORYMASTER_RECALL_DENSE_LOG"] = os.devnull  # evaluation calls are not production traffic
    from memorymaster.core.service import MemoryService
    from memorymaster.recall import context_hook as hook

    MemoryService._record_accesses = lambda *a, **k: None  # read-only evaluation
    hook._emit_recall_latency = lambda *a, **k: None
    hook._jev_recall_rendering = lambda *a, **k: None
    classify = hook._classify_query_type
    hook._route_recall_query = lambda query, _data: classify(query, jev=False)

    root = Path(args.set)
    queries = json.loads((root / "queries.json").read_text(encoding="utf-8"))
    labels: dict[tuple[int, int], int] = {}
    for line in (root / "labels.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        labels[(int(row["q"]), int(row["claim_id"]))] = int(row["label"])
    by_query: dict[int, list[int]] = {}
    for (q, _cid), value in labels.items():
        by_query.setdefault(q, []).append(value)

    ndcgs, useful, injected, noise, chars, unlabeled, latency = [], [], [], [], [], 0, []
    to_label: list[dict] = []
    store = MemoryService(args.db, workspace_root=Path(args.db).parent, read_only=True).store
    for query in queries:
        data = {"cwd": query["cwd"], "session_id": "eval"} if query.get("cwd") else None
        started = time.perf_counter()
        text, ids = hook.recall(query["q"], db_path=args.db, return_ids=True, hook_data=data)
        latency.append((time.perf_counter() - started) * 1000)
        gains = []
        missing = []
        for cid in ids:
            label = labels.get((query["i"], cid))
            unlabeled += label is None
            gains.append(label or 0)
            if label is None:
                missing.append({"claim_id": cid, "text": store.get_claim(cid, include_citations=False).text[:500]})
        if missing:
            to_label.append({"q": query["i"], "query": query["q"], "candidates": missing})
        score = _ndcg(gains, by_query.get(query["i"], []))
        if score is not None:
            ndcgs.append(score)
        useful.append(sum(g >= 1 for g in gains))
        injected.append(len(ids))
        noise.append(sum(g == 0 for g in gains))
        chars.append(len(text))

    total = sum(injected)
    summary = {
        "mode": args.mode,
        "prompts": len(queries),
        "ndcg10": round(statistics.mean(ndcgs), 3),
        "useful_per_prompt": round(statistics.mean(useful), 2),
        "injected_per_prompt": round(statistics.mean(injected), 2),
        "useless_share": round(sum(noise) / total, 3) if total else None,
        "chars_per_prompt": round(statistics.mean(chars)),
        "unlabeled_share": round(unlabeled / total, 3) if total else 0.0,
        "latency_ms_p50": round(statistics.median(latency), 1),
    }
    print(json.dumps(summary, indent=2))
    if args.unlabeled_out:
        Path(args.unlabeled_out).write_text(json.dumps(to_label, ensure_ascii=False), encoding="utf-8")
    if args.out:
        with open(args.out, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **summary}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
