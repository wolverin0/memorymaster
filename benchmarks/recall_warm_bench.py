"""Warm, cold and after-write latency of context_hook.recall with an exact ranking guard (campaign D).

The shared MCP server answers prompt recall warm; live (2026-10-04) its hook_recall took
a 660 ms median. This times recall() in one long-lived process over a disposable
synthetic corpus, separating:
  cold   first call of the process,
  warm   repeated calls with an unchanged corpus,
  write  the first call after one claim is written (the corpus generation moves).
Each query's ordered claim ids are fingerprinted: a candidate is admissible only if
every fingerprint is unchanged. Live configuration and home are removed.

    python benchmarks/recall_warm_bench.py --size 5000 --repeats 7 > baseline.json
    python benchmarks/recall_warm_bench.py --size 5000 --guard baseline.json
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.util
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUERIES = ("why does the hermes sync quarantine rows every cycle",
           "cómo se configura el backup de la base sqlite",
           "mikrotik router failover dns cloudflare tunnel",
           "steward decision on duplicate claims in project scope",
           "gitnexus serena profile dreaming capture spool lease")


def _isolate(tmp: Path) -> None:
    for name in list(os.environ):
        if name.startswith("MEMORYMASTER_") or name in {"GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                                                         "TYPESAFE_API_KEY", "QDRANT_URL"}:
            del os.environ[name]
    os.environ.update(HOME=str(tmp), USERPROFILE=str(tmp), MEMORYMASTER_LLM_RERANK="0",
                      MEMORYMASTER_RECALL_RERANK="0", MEMORYMASTER_DECISIONS_DB=str(tmp / "decisions.db"),
                      MEMORYMASTER_RECALL_STATE_DIR=str(tmp / "recall-state"), MEMORYMASTER_SPOOL_DIR=str(tmp / "spool"))


def _builder():
    spec = importlib.util.spec_from_file_location("bench_corpus", ROOT / "benchmarks" / "tokenizer_cold_bench.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.cached_corpus


def _quantiles(samples: list[float]) -> dict:
    samples = sorted(samples)
    return {"median_ms": round(statistics.median(samples), 2),
            "p95_ms": round(samples[min(len(samples) - 1, int(0.95 * len(samples)))], 2),
            "max_ms": round(samples[-1], 2), "n": len(samples)}


def measure(size: int, repeats: int, confirmed_share: float = 0.1, cache_dir: str | None = None) -> dict:
    saved = dict(os.environ)
    try:
        return _measure(size, repeats, confirmed_share, cache_dir)
    finally:  # the isolation must not outlive the run (pytest runs other tests in this process)
        os.environ.clear()
        os.environ.update(saved)


def _measure(size: int, repeats: int, confirmed_share: float, cache_dir: str | None) -> dict:
    with tempfile.TemporaryDirectory(prefix="mm-recall-bench-", ignore_cleanup_errors=True) as tmp_name:
        tmp = Path(tmp_name)
        _isolate(tmp)
        sys.path.insert(0, str(ROOT))
        db = tmp / "corpus.db"
        build_seconds = _builder()(db, size, cache_dir, confirm_share=confirmed_share)
        from memorymaster.core.models import CitationInput
        from memorymaster.core.service import MemoryService
        from memorymaster.recall.context_hook import recall

        hook_data = {"cwd": str(tmp), "session_id": "bench"}

        def call(query: str) -> tuple[float, list[int]]:
            started = time.perf_counter()
            _ctx, ids = recall(query, db_path=str(db), skip_qdrant=True, return_ids=True, hook_data=hook_data)
            return (time.perf_counter() - started) * 1000, ids

        cold_ms, _ = call(QUERIES[0])
        for query in QUERIES:
            call(query)  # warmup
        warm, fingerprints = [], {}
        for _ in range(repeats):
            for query in QUERIES:
                ms, ids = call(query)
                warm.append(ms)
                fingerprints[query] = hashlib.sha256(json.dumps(ids).encode()).hexdigest()
        writer = MemoryService(db, workspace_root=tmp)
        after_write = []
        for r in range(repeats):
            writer.store.create_claim(text=f"bench write {r} sqlite backup", citations=[CitationInput(source="bench://w")],
                                      scope="project:p1")
            ms, _ = call(QUERIES[r % len(QUERIES)])
            after_write.append(ms)
        after_update = []  # the write the steward and validators make all day
        for r in range(repeats):
            with writer.store.connect() as conn:
                conn.execute("UPDATE claims SET confidence = ?, updated_at = ? WHERE id = ?",
                             (0.5 + r / 100, f"2026-10-04T00:00:{r:02d}Z", 1 + r))
            ms, _ = call(QUERIES[r % len(QUERIES)])
            after_update.append(ms)
        del writer
        gc.collect()
    warm_q = _quantiles(warm)
    return {"schema": "memorymaster.bench.recall_warm.v1", "size": size, "repeats": repeats,
            "confirmed_share": confirmed_share,
            "build_seconds": round(build_seconds, 1), "metric": warm_q["p95_ms"],
            "cold_ms": round(cold_ms, 1), "warm": warm_q, "after_write": _quantiles(after_write), "after_update": _quantiles(after_update),
            "returned_any": any(fp != hashlib.sha256(b"[]").hexdigest() for fp in fingerprints.values()),
            "fingerprints": fingerprints}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--size", type=int, default=5000)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--confirmed-share", type=float, default=0.1,
                        help="share of claims confirmed (live 2026-10-04: about 4.8k of 46k live)")
    parser.add_argument("--guard", help="baseline JSON; exit 1 if any query's ranked ids changed")
    parser.add_argument("--corpus-cache", help="directory to reuse built corpora across runs (copied per run)")
    args = parser.parse_args(argv)
    result = measure(args.size, max(1, args.repeats), args.confirmed_share, args.corpus_cache)
    if args.guard:
        baseline = json.loads(Path(args.guard).read_text(encoding="utf-8"))
        violations = [f"{q}: ranked ids changed" for q, fp in baseline["fingerprints"].items()
                      if result["fingerprints"].get(q) != fp]
        result.update(verdict="fail" if violations else "pass", violations=violations)
        print(json.dumps(result))  # ASCII: Windows stdout is not UTF-8
        return 1 if violations else 0
    print(json.dumps(result))  # ASCII: Windows stdout is not UTF-8
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
