"""Retrieval quality of the deliberate query_memory path on a fixed question set.

Every question carries its known target claim id(s). The script runs each one
through ``MemoryService.retrieve`` (the exact path behind the ``query_memory``
MCP tool) in ``trust_mode="trusted"`` with ``limit=20`` and reports hit@k,
MRR, zero-result rate and p50/p95 latency, per retrieval mode and per question
provenance ("transcript" = real past tool call, "claim-derived" = written from
a confirmed claim and paraphrased; the latter inflates hit@k, so the groups are
never merged into one headline number).

Read-only: the store is opened read-only, so the result cache is off (recall
access lines still go to the spool, keyed to the snapshot). Point ``--db`` at a snapshot made with the sqlite3 backup
API, never at the authoritative database. The set holds real operator text and
lives outside the repository:
~/.memorymaster/evals/recall-quality-20261013/{questions.json,results-<mode>.json}.

    python scripts/eval_query_memory_quality.py --mode legacy --db <snapshot.db>
    python scripts/eval_query_memory_quality.py --mode hybrid --db <snapshot.db> --out results-hybrid.json

``legacy`` is FTS/lexical ranking, ``hybrid`` adds local sentence-transformer
vectors. ``qdrant`` is not measured: the planner contains it to ``legacy``
unless MEMORYMASTER_QDRANT_GOVERNED_READS is set, and that needs a live service.
(``scripts/eval_recall_quality.py`` is the older 30-prompt hook-tokenizer eval and
``scripts/eval_prompt_recall.py`` covers the prompt hook; this script targets the deliberate path.)
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Iterable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DEFAULT_SET = Path.home() / ".memorymaster" / "evals" / "recall-quality-20261013" / "questions.json"
MODES = ("legacy", "hybrid")
LIMIT = 20  # query_memory's default depth, so zero-result rates are comparable


def hit_at_k(returned_ids: list[int], targets: Iterable[int], k: int) -> bool:
    """True when any target claim is among the first ``k`` returned ids."""
    wanted = set(targets)
    return any(cid in wanted for cid in returned_ids[:k])


def target_rank(returned_ids: list[int], targets: Iterable[int]) -> int | None:
    """1-based rank of the best-placed target, ``None`` when it is not returned."""
    wanted = set(targets)
    for position, cid in enumerate(returned_ids, start=1):
        if cid in wanted:
            return position
    return None


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile (no interpolation); ``None`` for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize(rows: list[dict[str, Any]], k: int) -> dict[str, Any]:
    """Aggregate per-question rows (``rank``, ``returned``, ``latency_ms``)."""
    n = len(rows)
    if not n:
        return {"n": 0}
    latency = [float(r["latency_ms"]) for r in rows]
    ranks = [r["rank"] for r in rows]
    return {
        "n": n,
        f"hit_at_{k}": round(sum(1 for r in ranks if r is not None and r <= k) / n, 3),
        "hit_at_any": round(sum(1 for r in ranks if r is not None) / n, 3),
        "mrr": round(sum(1 / r for r in ranks if r) / n, 3),
        "zero_result_rate": round(sum(1 for r in rows if r["returned"] == 0) / n, 3),
        "mean_results": round(statistics.mean(r["returned"] for r in rows), 2),
        "latency_ms_p50": round(percentile(latency, 50), 1),
        "latency_ms_p95": round(percentile(latency, 95), 1),
        "latency_ms_max": round(max(latency), 1),
    }


def evaluate(svc: Any, items: list[dict[str, Any]], mode: str, k: int, limit: int = LIMIT) -> list[dict[str, Any]]:
    from memorymaster.recall.planner import RetrievalRequest

    rows: list[dict[str, Any]] = []
    for item in items:
        scope = item.get("scope")
        request = RetrievalRequest(
            query_text=item["question"],
            limit=limit,
            trust_mode="trusted",
            retrieval_mode=mode,
            scope_allowlist=tuple(scope) if scope else None,
        )
        started = time.perf_counter()
        result = svc.retrieve(request)
        elapsed = (time.perf_counter() - started) * 1000
        returned = [int(row["claim"].id) for row in result.rows]
        targets = item["target_claim_ids"]
        rows.append({
            "id": item["id"],
            "provenance": item["provenance"],
            "rank": target_rank(returned, targets),
            "hit": hit_at_k(returned, targets, k),
            "returned": len(returned),
            "latency_ms": round(elapsed, 1),
            "top_ids": returned[:k],
            "effective_mode": result.plan.effective_mode,
        })
    return rows


def effective_config(svc: Any, db: str) -> dict[str, Any]:
    from memorymaster.core import service as service_module
    from memorymaster.recall import graph_expansion, query_cache

    config: dict[str, Any] = {
        "db": Path(db).name,
        "store_read_only": bool(getattr(svc.store, "read_only", False)),
        "result_cache_enabled": bool(query_cache.cache_enabled()),
        "graph_mode": graph_expansion.resolve_graph_mode(None),
        "llm_rerank": bool(service_module._llm_rerank_enabled()),
    }
    provider = svc.embedding_provider
    config["embedding_model"] = provider.model
    config["embedding_semantic"] = bool(getattr(provider, "is_semantic", False))
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--set", default=str(DEFAULT_SET), help="questions.json (or its directory)")
    parser.add_argument("--db", required=True, help="snapshot database; never the authoritative one")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--limit", type=int, default=LIMIT)
    parser.add_argument("--out", default=None, help="also write the full per-question results here")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    db = str(Path(args.db).resolve())
    set_path = Path(args.set)
    if set_path.is_dir():
        set_path = set_path / "questions.json"
    document = json.loads(set_path.read_text(encoding="utf-8"))
    items = document["items"]

    from memorymaster.core.service import MemoryService

    svc = MemoryService(db, workspace_root=Path(db).parent, read_only=True)
    config = effective_config(svc, db)
    warmup_ms = None
    if args.mode == "hybrid":
        # One untimed query loads the embedding model, as a long-lived server already has.
        started = time.perf_counter()
        evaluate(svc, items[:1], args.mode, args.k, args.limit)
        warmup_ms = round((time.perf_counter() - started) * 1000, 1)
    rows = evaluate(svc, items, args.mode, args.k, args.limit)

    provenances = sorted({r["provenance"] for r in rows})
    summary = {
        "mode": args.mode,
        "k": args.k,
        "limit": args.limit,
        "set": set_path.name,
        "set_size": len(items),
        "warmup_ms": warmup_ms,
        "config": config,
        "overall": summarize(rows, args.k),
        "by_provenance": {p: summarize([r for r in rows if r["provenance"] == p], args.k) for p in provenances},
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if args.out:
        Path(args.out).write_text(
            json.dumps({**summary, "items": rows}, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
