# MemoryMaster vNext pre-change baseline
# Covers: reproducible retrieval, test, capture, graph, scheduler, size, and latency evidence.
# Key terms: d33a268, LongMemEval-S, R@5, MRR, collection, wheel, perf smoke.
# Read when: evaluating vNext regressions or deciding release-candidate readiness.
# Scope: clean worktree from origin/main; temporary databases only; no live activation.
# Recorded: 2026-07-27 on Windows, Python 3.12.4.
# Status: baseline evidence, not a launch or deployment verdict.

## Source

- Commit: `d33a2687ed8cd53f25e0f655e91c1fb9f036eb7f`.
- GitNexus: 966 files, 12,836 symbols, 34,659 relationships, 300 flows,
  9,784 embeddings; index commit matched HEAD.
- Test collection: 4,290 tests in 12.01 seconds.
- Focused Atlas/entity/scheduler baseline: 53 passed in 16.48 seconds.

## Retrieval and QA

The current committed `benchmark/longmemeval_s_results.json` is a
retrieval-only, 500-question LongMemEval-S cleaned run:

| Metric | Value |
| --- | ---: |
| R@5 | 0.9660 |
| R@10 | 0.9840 |
| MRR | 0.9020579365 |

No comparable full OAuth-backed QA run is present in this clean checkout.
Release comparison must therefore run the same judge before and after; older
partial QA values are not an acceptable baseline.

## Performance and package

`python benchmarks/perf_smoke.py` passed:

| Metric | Value |
| --- | ---: |
| Total runtime | 7.172 s |
| Ingest p95 | 0.023109 s |
| Ingest throughput | 49.23 ops/s |
| Query p95 | 0.043291 s |
| Query throughput | 28.65 ops/s |
| Cycle p95 | 2.863414 s |

- Tracked tree: 1,085 files, 31,226,356 bytes (29.78 MiB).
- Default wheel: 870,416 bytes, SHA-256
  `751b4ced64c0c24f6ada18b66d5000125f6866b11cedfb1f3c5783ac34ffcbee`.

## Capture, graph, and scheduler behavior

- Atlas source items are idempotent by `(source_id, source_item_id)`.
- Evidence rows have no content hash and no claim/evidence relation.
- Typed Atlas extraction emits candidate claims with `evidence:<id>`
  citation locators.
- Canonical entity edges currently carry one `claim_id`; repeated support and
  retired-source authorization are not modeled relationally.
- `run_cycle` owns existing steward/scheduler work and LLM budgets.
- There is no public capture-job queue or `remember / forget / improve` facade.

## Private evaluation corpus

The untracked evaluation fixture lives at
`~/.memorymaster/evals/vnext-governed-capture-v1/eval.jsonl`. It contains only
synthetic release-gate cases and must never be committed or copied into
artifacts.
