"""Benchmark: recall latency + init_db cost against the LIVE memorymaster.db.

Replicates exactly what ~/.claude/hooks/memorymaster-recall.py does:
    from memorymaster.context_hook import recall
    recall(query, db_path=DB_PATH, skip_qdrant=True)

READ-ONLY discipline: recall() is a pure read path. init_db() is idempotent DDL
(CREATE TABLE/INDEX IF NOT EXISTS) -- same as MCP server boot. No ingest calls.
"""
import json
import os
import statistics
import sys
import time

PROJECT_ROOT = r"G:\_OneDrive\OneDrive\Desktop\Py Apps\memorymaster"
DB_PATH = os.path.join(PROJECT_ROOT, "memorymaster.db")

sys.path.insert(0, PROJECT_ROOT)
os.environ["MEMORYMASTER_DEFAULT_DB"] = DB_PATH
os.chdir(PROJECT_ROOT)

QUERIES = [
    "steward validation cycle batch limit",
    "postgres parity sqlite schema sync",
    "recall fusion FTS5 vector ranking",
    "sensitivity filter ingest path secrets",
    "wiki absorb compiled truth timeline articles",
    "qdrant vector search embeddings down fallback",
    "claim lifecycle status superseded conflicted",
    "dream bridge auto ingest transcript learnings",
    "mcp server tool auto citation source agent",
    "WAL mode database corruption concurrent access",
    "decay job stale claims freshness window",
    "tier recompute core working peripheral recall weight",
    "openclaw sync bidirectional merge claims",
    "context hook recall budget token limit",
    "conflict resolver supersedes replaced by claim id",
    "scope filter project user team global query",
    "fts5 full text search tokenizer match query",
    "llm provider gemini openai anthropic ollama routing",
    "vault linter contradictions orphans gaps health",
    "compaction summaries archive dedup claims",
    "hook recall latency user prompt submit performance",
    "bitemporal valid from valid until event time",
    "session start hook inject recent claims candidates",
    "steward proposals resolve promote candidate confirmed",
]


def main():
    from memorymaster.context_hook import recall

    # Warm-up call (includes lazy module init, first-connection cost) -- reported
    # separately, excluded from p50/p95.
    t0 = time.perf_counter()
    warm_ctx = recall("warmup query about memorymaster architecture", db_path=DB_PATH, skip_qdrant=True)
    warmup_ms = (time.perf_counter() - t0) * 1000

    timings = []
    errors = []
    hits = 0
    for q in QUERIES:
        t0 = time.perf_counter()
        try:
            ctx = recall(q, db_path=DB_PATH, skip_qdrant=True)
            elapsed = (time.perf_counter() - t0) * 1000
            timings.append(round(elapsed, 2))
            if ctx and ctx.strip():
                hits += 1
        except Exception as e:
            errors.append(f"{q}: {type(e).__name__}: {e}")

    timings_sorted = sorted(timings)
    n = len(timings_sorted)
    p50 = statistics.median(timings_sorted)
    # p95 via nearest-rank
    p95 = timings_sorted[max(0, min(n - 1, int(round(0.95 * n)) - 1))]

    # INIT: MemoryService(db_target=...).init_db() -- time call 1 (cold-ish) and
    # call 2 (warm = MCP boot cost on an already-initialized DB).
    from memorymaster.service import MemoryService
    init_err = None
    init1_s = init2_s = None
    try:
        svc = MemoryService(DB_PATH)
        t0 = time.perf_counter()
        svc.init_db()
        init1_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        svc.init_db()
        init2_s = time.perf_counter() - t0
    except Exception as e:
        init_err = f"{type(e).__name__}: {e}"

    print(json.dumps({
        "samples": n,
        "recall_p50_ms": round(p50, 2),
        "recall_p95_ms": round(p95, 2),
        "min_ms": timings_sorted[0] if n else None,
        "max_ms": timings_sorted[-1] if n else None,
        "mean_ms": round(statistics.mean(timings_sorted), 2) if n else None,
        "warmup_first_call_ms": round(warmup_ms, 2),
        "warmup_hit": bool(warm_ctx and warm_ctx.strip()),
        "hits": hits,
        "all_timings_ms": timings,
        "errors": errors,
        "init_db_call1_s": round(init1_s, 3) if init1_s is not None else None,
        "init_db_call2_warm_s": round(init2_s, 3) if init2_s is not None else None,
        "init_error": init_err,
    }, indent=2))


if __name__ == "__main__":
    main()
