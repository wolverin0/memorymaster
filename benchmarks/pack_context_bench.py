"""Latency and exact-output fingerprint of recall.context_optimizer.pack_context (campaign B).

Pure: synthetic, seeded rows; no database, network or provider. Every (rows, format)
case reports wall-time quantiles over independent repeats and a sha256 over the
packed output, ``claims_included`` and ``tokens_used``. A candidate optimisation is
admissible only if every fingerprint equals the baseline's (the Guard); the
primary metric is ``p95_ms`` of the largest case.

    python benchmarks/pack_context_bench.py --repeats 7 > baseline.json
    python benchmarks/pack_context_bench.py --guard baseline.json   # exit 1 on any drift
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memorymaster.core.models import Citation, Claim  # noqa: E402
from memorymaster.recall.context_optimizer import OUTPUT_FORMATS, pack_context  # noqa: E402

SIZES = (20, 50, 200)
BUDGET = 16_000  # large enough that most rows fit: the quadratic path is exercised
WORDS = ("cloudflare tunnel dns record failover sqlite wal checkpoint ingest recall steward claim "
         "citation scope tenant ñandú decisión configuración backup restore hermes sync delta").split()


def _rows(n: int, seed: int = 20261004) -> list[dict]:
    rng = random.Random(seed + n)
    rows = []
    for i in range(1, n + 1):
        text = " ".join(rng.choice(WORDS) for _ in range(rng.randint(8, 60)))
        citations = [Citation(id=i * 10 + k, claim_id=i, source=f"session://{rng.randint(1, 99)}",
                              locator=f"turn:{rng.randint(1, 400)}", excerpt=text[:80], created_at="2026-10-01T00:00:00Z")
                     for k in range(rng.randint(0, 3))]
        claim = Claim(id=i, text=text, idempotency_key=None, normalized_text=None, claim_type="fact",
                      subject=rng.choice(WORDS), predicate="relates_to", object_value=rng.choice(WORDS),
                      scope="project:bench", volatility="medium", status=rng.choice(("confirmed", "candidate", "stale")),
                      confidence=round(rng.uniform(0.3, 0.99), 3), pinned=rng.random() < 0.05,
                      supersedes_claim_id=None, replaced_by_claim_id=None, created_at="2026-09-01T00:00:00Z",
                      updated_at="2026-10-01T00:00:00Z", last_validated_at=None, archived_at=None, citations=citations)
        score = round(1.0 - i / (n + 1), 6)
        rows.append({"claim": claim, "score": score, "lexical_score": 0.5, "freshness_score": 0.8,
                     "confidence_score": claim.confidence, "vector_score": 0.0,
                     "annotation": {"status": claim.status, "active": True, "stale": claim.status == "stale",
                                    "conflicted": False, "pinned": claim.pinned}})
    return rows


def _fingerprint(result) -> str:
    payload = json.dumps([result.output, result.claims_included, result.tokens_used], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def measure(repeats: int) -> dict:
    cases = {}
    for n in SIZES:
        rows = _rows(n)
        for fmt in OUTPUT_FORMATS:
            pack_context(rows, token_budget=BUDGET, output_format=fmt)  # warmup
            samples = []
            for _ in range(repeats):
                started = time.perf_counter()
                result = pack_context(rows, token_budget=BUDGET, output_format=fmt)
                samples.append((time.perf_counter() - started) * 1000)
            samples.sort()
            cases[f"{n}:{fmt}"] = {
                "median_ms": round(statistics.median(samples), 3),
                "p95_ms": round(samples[min(len(samples) - 1, int(0.95 * len(samples)))], 3),
                "min_ms": round(samples[0], 3), "max_ms": round(samples[-1], 3),
                "claims_included": result.claims_included, "tokens_used": result.tokens_used,
                "fingerprint": _fingerprint(result),
            }
    largest = f"{SIZES[-1]}:json"
    return {"schema": "memorymaster.bench.pack_context.v1", "repeats": repeats, "budget": BUDGET,
            "metric": cases[largest]["p95_ms"],  # top-level scalar for codex-autoresearch --metric-key metric
            "primary": {"case": largest, "p95_ms": cases[largest]["p95_ms"]}, "cases": cases}


def guard(current: dict, baseline: dict) -> list[str]:
    return [f"{case}: fingerprint changed" for case, row in baseline["cases"].items()
            if current["cases"].get(case, {}).get("fingerprint") != row["fingerprint"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--guard", help="baseline JSON; exit 1 if any output fingerprint differs")
    args = parser.parse_args(argv)
    result = measure(max(1, args.repeats))
    if args.guard:
        violations = guard(result, json.loads(Path(args.guard).read_text(encoding="utf-8")))
        result.update(verdict="fail" if violations else "pass", violations=violations)
        print(json.dumps(result))  # ASCII: Windows stdout is not UTF-8
        return 1 if violations else 0
    print(json.dumps(result))  # ASCII: Windows stdout is not UTF-8
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
