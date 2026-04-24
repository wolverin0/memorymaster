# LongMemEval — per-question DB isolation (roadmap 11.4)

Date: 2026-04-24
Branch: `omni/feat-longmemeval-per-q-iso-2026-04-24`
Base: commit `7740763` (post 11.1 LLM fallback merge)
Related claim: 11896 (cross-question contamination is the sole failure mode)
Harness: `scripts/run_longmemeval.py` with new `--isolate-per-q` and `--keep-bench-dbs` flags
Dataset: LongMemEval oracle — 500 questions
Run command (isolated): `python scripts/run_longmemeval.py --limit 0 --config-label isolated --isolate-per-q --output-dir artifacts/longmemeval-per-q`
Run command (shared): `python scripts/run_longmemeval.py --limit 0 --config-label shared --output-dir artifacts/longmemeval-per-q`

## Why

Pre-11.4, the harness seeded ALL 500 questions into ONE `bench-<config>.db`. When
scoring hit@k per question, FTS5 candidate generation, BM25 re-ranking, entity
fanout, and vector fallback all saw claims from OTHER questions too. A
post-hoc inspection of the 500-Q baseline-A results confirmed claim 11896's
prediction:

```
misses = 285/500  (hit@5 == 0)
cross-question top-1 = 285/285 = 100.0%
```

Every single miss had its top-1 retrieved from a claim that belongs to a
different question's haystack. Cross-question contamination is not a factor —
it is THE failure mode on this harness. In production MemoryMaster, claims
from multiple projects co-exist in one DB but are filtered by `scope`; the
LongMemEval harness was never applying an equivalent per-question filter
during FTS5 candidate generation (only during final session-ID scoring).

## What changed

`scripts/run_longmemeval.py` now accepts two new flags (opt-in, backward
compatible with the legacy A/B/C/D sweep):

| Flag | Behaviour |
|------|-----------|
| `--isolate-per-q` | For each question, open a fresh SQLite bench DB under `%TEMP%/memorymaster-longmemeval/<uuid>/<qid>.db`, seed only that question's haystack sessions, run recall, score, then unlink. Eliminates cross-question contamination at the storage layer. |
| `--keep-bench-dbs` | With `--isolate-per-q`, persist each per-question DB under `artifacts/longmemeval-per-q/<qid>.db` instead of cleaning up. Debugging aid only. |

Default behaviour (shared `bench-<config>.db` across all 500 Qs) is unchanged,
so the existing `--configs A,B,C,D` sweep still produces bit-identical
numbers. The worker subprocess picks up the knobs via two new env vars:
`MEMORYMASTER_LONGMEMEVAL_ISOLATE_PER_Q=1` and
`MEMORYMASTER_LONGMEMEVAL_KEEP_DBS=1`.

The `memorymaster/` package was NOT touched — per-Q isolation lives entirely
in the harness. Roadmap 11.7 (mirroring per-scope filtering into the
production recall path) remains a separate PR.

## Results

| Config | hit@1 | hit@5 | MRR | mean latency | total runtime |
|---|---|---|---|---|---|
| shared DB (baseline, current) | 0.342 | 0.430 | 0.377 | 67.6ms | 142.7s |
| **per-Q iso (new)** | **0.998** | **0.998** | **0.998** | **119.2ms** | **228.4s** |
| delta | +0.656 | +0.568 | +0.621 | +51.6ms | +85.7s |

Acceptance gate was **hit@5 lift >= 0.10**. Measured lift is **+0.568**, 5.7x
over the bar. Claim 11896 is confirmed, and then some: once the ranker can
only see the correct question's claims, the ranker-as-implemented reaches
functional ceiling on this dataset. The 1 remaining miss (`gpt4_8279ba03`,
temporal-reasoning) retrieved an empty result — a tokenizer / query-rewrite
issue, not contamination.

## Interpretation

- The +0.568 hit@5 delta is a clean experimental isolation of "cross-question
  contamination cost" on this harness. It is NOT an upper bound on production
  recall quality; production uses `scope`-based filtering and doesn't suffer
  this bug in the first place.
- The lift is not a shippable feature — it measures what the inline
  harness ranker does when it's given a correctly-scoped corpus. The
  conclusion is that the LongMemEval A/B/C/D results from
  `artifacts/longmemeval-2026-04-24.md` are mechanically dominated by
  contamination noise, and future recall experiments on LongMemEval MUST
  default to `--isolate-per-q` or risk comparing noise to noise.
- Latency rises from 67.6ms to 119.2ms per question (+51.6ms). The overhead
  is dominated by per-Q `svc.init_db()` (SQLite schema creation + FTS5 + WAL
  setup). Total runtime rose from 142.7s to 228.4s (+60%). For a benchmark
  run this is fine; it would be unacceptable in a production hot path, but
  that's not what this harness measures.
- Cleanup is reliable: the `%TEMP%/memorymaster-longmemeval/` root is empty
  after the 500-Q run. No files leak into the repo tree. Verified on
  Windows + WAL by forcing `gc.collect()` after each question's service is
  dereferenced (Windows holds sqlite file handles longer than POSIX).

## Unexpected finding worth a claim

Per-Q isolation reached **hit@1 = 0.998** AND **hit@5 = 0.998** — the two
metrics are identical. In other words, when contamination is removed, the
ranker almost always lands the correct session at rank 1; adding rank-2..5
slots adds no measurable lift. This means the harness's inline BM25 + linear
fusion ranker is operating AT ceiling on a clean corpus, and any future
ranker experiment on LongMemEval must use `--isolate-per-q` to leave
headroom visible. Running A/B/C/D sweeps in shared-DB mode was effectively
measuring "how much contamination noise does config X suppress" rather than
"how good is the ranker". Worth a claim + a roadmap note for 11.7 follow-up.

## Files

- `scripts/run_longmemeval.py` — modified: adds `--isolate-per-q`,
  `--keep-bench-dbs`, `_per_q_root`, `_per_q_db_path`, `_cleanup_per_q_db`.
- `artifacts/longmemeval-per-q/summary-shared.json` — 500-Q baseline summary.
- `artifacts/longmemeval-per-q/results-shared.jsonl` — per-Q records.
- `artifacts/longmemeval-per-q/summary-isolated.json` — 500-Q isolated summary.
- `artifacts/longmemeval-per-q/results-isolated.jsonl` — per-Q records.

## Verification

- `python -m pytest tests/ -q --tb=short` — 1574 passed, 40 skipped, 1 xfailed (same counts as main).
- `ruff check scripts/run_longmemeval.py` — clean.
- `python scripts/run_longmemeval.py --limit 5 --config-label smoke-iso --isolate-per-q` — 5/5 hits; temp dir cleaned.
