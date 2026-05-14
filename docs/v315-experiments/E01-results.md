# EXPERIMENT v315-E01 - Enable vector signal in LongMemEval bench harness

## Hypothesis

`tests/bench_longmemeval.py` initializes an ephemeral MemoryService that has `QDRANT_URL` popped and uses `hash-v1` fallback embeddings (no semantic content). The retrieval pipeline's W_VEC weight (~0.10) multiplies a near-zero vector score, so the vector stream contributes nothing in the current 0.894 R@5 number. Wiring `embeddings.create_best_provider()` (which auto-detects sentence-transformers `all-MiniLM-L6-v2`) into the bench should restore real semantic content and lift R@5.

**Predicted Delta R@5: +0.02 to +0.04.** Honest-null acceptance: if it doesn't move, ship the null finding.

## What Changed

File: `tests/bench_longmemeval.py`

- Imports `create_best_provider`.
- Initializes one embedding provider before the retrieval question loop.
- Reuses that provider across ephemeral MemoryService instances.
- Assigns the provider via `service.embedding_provider`.
- Removes the disabled `vector_hook` override so `query_rows(..., retrieval_mode="hybrid")` uses the store vector scorer.

## Metrics

Pre-experiment baseline from `benchmark/longmemeval_s_results.json` on origin/main:

| Metric | Baseline |
| --- | ---: |
| R@5 | 0.8940 |
| R@10 | 0.9420 |
| MRR | 0.7992 |

Post-experiment full 500-question retrieval run:

| Metric | Post |
| --- | ---: |
| R@5 | 0.9660 |
| R@10 | 0.9840 |
| MRR | 0.9021 |

Delta:

| Metric | Delta |
| --- | ---: |
| R@5 | +0.0720 |
| R@10 | +0.0420 |
| MRR | +0.1029 |

## Decision

KEEP, pending pytest gate completion. The benchmark lift exceeds the R@5 >= +0.005 KEEP threshold.

## Verification

- `python tests/bench_longmemeval.py --retrieval-only`: completed 500/500 and printed R@5=0.9660, R@10=0.9840, MRR=0.9021; PowerShell then reported a console-title pipe error after results were written.
- `python -m pytest tests/ -q --tb=line -x`: blocked by Windows process initialization failure (`0xC0000142`) before pytest produced output.

## What Next

Run the pytest gate after command execution recovers; then proceed to the next isolated retrieval-weight experiment.
