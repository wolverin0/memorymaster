"""Cold and warm cost of recall_tokenizer.extract_query_tokens by corpus size (campaign A).

The prompt hook runs as a fresh process, so it pays `_corpus_stats` (a scan and
tokenisation of every live claim) on every prompt; the shared server pays it once
per corpus generation. This builds disposable SQLite corpora of increasing size,
then times the first (cold) and second (warm) call in independent child processes
with the live configuration removed. The selected tokens are fingerprinted: a
candidate optimisation is admissible only if every fingerprint is unchanged.

    python benchmarks/tokenizer_cold_bench.py --sizes 1000 5000 --repeats 7 > baseline.json
    python benchmarks/tokenizer_cold_bench.py --sizes 1000 5000 --guard baseline.json
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import random
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORDS = ("cloudflare tunnel dns failover sqlite wal checkpoint ingest recall steward citation scope tenant "
         "hermes sync delta backup restore mikrotik router wisp billing afip factura whatsapp bot orca pane "
         "gitnexus serena profile dreaming capture spool lease quarantine schema migration decision").split()
QUERIES = ("why does the hermes sync quarantine rows every cycle",
           "cómo se configura el backup de la base sqlite",
           "mikrotik router failover dns cloudflare tunnel",
           "steward decision on duplicate claims in project scope")
CHILD = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
from memorymaster.recall.recall_tokenizer import extract_query_tokens
db, queries = sys.argv[2], json.loads(sys.argv[3])
t0 = time.perf_counter(); first = extract_query_tokens(queries[0], db); cold = time.perf_counter() - t0
t1 = time.perf_counter(); second = extract_query_tokens(queries[1], db); warm = time.perf_counter() - t1
print(json.dumps({"cold_ms": cold * 1000, "warm_ms": warm * 1000, "tokens": [first, second]}))
"""


def _hermetic_env(tmp: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYMASTER_")
           and k not in {"GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TYPESAFE_API_KEY", "QDRANT_URL"}}
    env.update(HOME=str(tmp), USERPROFILE=str(tmp), PYTHONDONTWRITEBYTECODE="1")
    return env


# Twelve scopes, two of them the ones prompt recall always admits ("project", "global").
# Same draw as the original `project:p{1..12}` (one _randbelow(12)), so texts are unchanged.
SCOPES = ("project", "global", *(f"project:p{i}" for i in range(3, 13)))


def cached_corpus(db: Path, n: int, cache_dir: str | None, *, confirm_share: float = 0.0) -> float:
    """Copy a previously built corpus when ``cache_dir`` has it; build (and keep a copy) otherwise.

    Building 20k-45k claims takes minutes, too slow for an iteration loop. The corpus
    depends only on (n, seed, confirm_share) and the write path, not on recall code, so
    a campaign that edits recall code may reuse it; it is always copied, never shared.
    """
    if not cache_dir:
        return build_corpus(db, n, confirm_share=confirm_share)
    cached = Path(cache_dir) / f"corpus-{n}-{confirm_share:g}.db"
    if not cached.exists():
        seconds = build_corpus(db, n, confirm_share=confirm_share)
        cached.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(db, cached)
        return seconds
    shutil.copyfile(cached, db)
    return 0.0


def build_corpus(db: Path, n: int, seed: int = 20261004, *, confirm_share: float = 0.0) -> float:
    sys.path.insert(0, str(ROOT))
    from memorymaster.core.lifecycle import transition_claim
    from memorymaster.core.models import CitationInput
    from memorymaster.core.service import MemoryService

    rng = random.Random(seed + n)
    status_rng = random.Random(seed * 7 + n)  # separate stream: texts stay identical
    svc = MemoryService(db, workspace_root=db.parent)
    svc.init_db()
    started = time.perf_counter()
    for i in range(n):
        text = " ".join(rng.choice(WORDS) for _ in range(rng.randint(6, 40)))
        claim = svc.store.create_claim(text=f"{text} #{i}", citations=[CitationInput(source="bench://tokenizer")],
                                       scope=rng.choice(SCOPES))
        if confirm_share and status_rng.random() < confirm_share:  # recall serves confirmed only (planner.py:96)
            transition_claim(svc.store, claim.id, "confirmed", reason="bench corpus", event_type="validator")
    elapsed = time.perf_counter() - started
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # the copied cache must be self-contained
    conn.close()
    del svc
    gc.collect()  # release the build connections before child processes and cleanup (Windows)
    return elapsed


def measure(sizes: list[int], repeats: int, cache_dir: str | None = None) -> dict:
    saved = dict(os.environ)
    try:  # the corpus is built in this process: build it without the live configuration too
        for name in list(os.environ):
            if name.startswith("MEMORYMASTER_") or name in {"GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                                                             "TYPESAFE_API_KEY", "QDRANT_URL"}:
                del os.environ[name]
        return _measure(sizes, repeats, cache_dir)
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _measure(sizes: list[int], repeats: int, cache_dir: str | None) -> dict:
    cases = {}
    with tempfile.TemporaryDirectory(prefix="mm-tokenizer-bench-", ignore_cleanup_errors=True) as tmp_name:
        tmp = Path(tmp_name)
        env = _hermetic_env(tmp)
        for n in sizes:
            db = tmp / f"corpus-{n}.db"
            build_seconds = cached_corpus(db, n, cache_dir)
            cold, warm, tokens = [], [], None
            for r in range(repeats):
                queries = [QUERIES[r % len(QUERIES)], QUERIES[(r + 1) % len(QUERIES)]]
                out = subprocess.run([sys.executable, "-P", "-c", CHILD, str(ROOT), str(db), json.dumps(queries)],
                                     capture_output=True, text=True, env=env, timeout=600, check=True)
                row = json.loads(out.stdout.strip().splitlines()[-1])
                cold.append(row["cold_ms"])
                warm.append(row["warm_ms"])
                if r == 0:
                    tokens = row["tokens"]
            cold.sort()
            warm.sort()
            cases[str(n)] = {
                "cold_median_ms": round(statistics.median(cold), 2), "cold_max_ms": round(cold[-1], 2),
                "warm_median_ms": round(statistics.median(warm), 3), "build_seconds": round(build_seconds, 1),
                "fingerprint": hashlib.sha256(json.dumps(tokens, ensure_ascii=False).encode("utf-8")).hexdigest(),
            }
    largest = str(sizes[-1])
    return {"schema": "memorymaster.bench.tokenizer_cold.v1", "repeats": repeats,
            "metric": cases[largest]["cold_median_ms"],  # top-level scalar for --metric-key metric
            "primary": {"corpus": int(largest), "cold_median_ms": cases[largest]["cold_median_ms"]}, "cases": cases}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 5000])
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--guard", help="baseline JSON; exit 1 if any fingerprint differs")
    parser.add_argument("--corpus-cache", help="directory to reuse built corpora across runs (copied per run)")
    args = parser.parse_args(argv)
    result = measure(sorted(args.sizes), max(1, args.repeats), args.corpus_cache)
    if args.guard:
        baseline = json.loads(Path(args.guard).read_text(encoding="utf-8"))
        violations = [f"{n}: tokens changed" for n, row in baseline["cases"].items()
                      if result["cases"].get(n, {}).get("fingerprint") != row["fingerprint"]]
        result.update(verdict="fail" if violations else "pass", violations=violations)
        print(json.dumps(result))
        return 1 if violations else 0
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
