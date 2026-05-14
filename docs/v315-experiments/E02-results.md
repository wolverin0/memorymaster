# EXPERIMENT v315-E02 - RRF fusion in query_rows

## Hypothesis

`memorymaster/recall_fusion.py` already implements Reciprocal Rank Fusion (RRF, k=60) but is only invoked from `memorymaster/context_hook.py` for token-packing during context generation — NOT from the raw retrieval path in `memorymaster/service.py:query_rows`. Currently `query_rows` ranks via a linear weighted sum of (W_LEX·lex + W_CONF·conf + W_FRESH·fresh + W_VEC·vec) per `memorymaster/retrieval.py:rank_claim_rows`. Linear blends are brittle when signal magnitudes differ. RRF over the same component rankings is more robust per agentmemory's published architecture.

**Predicted Δ R@5: +0.01 to +0.02** on top of E01's 0.966 baseline. Honest-null acceptance: if it doesn't move or regresses, ship the null/REVERT finding.

## What Changed

Experimental implementation was applied, benchmarked, and then reverted because it crossed the REVERT threshold.

- `memorymaster/config.py`: added an opt-in `MEMORYMASTER_RRF_QUERY_ROWS` boolean, default off.
- `memorymaster/retrieval.py`: added `rank_claim_rows_rrf` as an alternate hybrid ranker.
- `memorymaster/retrieval.py`: reused the existing lexical, confidence, freshness, and vector component scores.
- `memorymaster/retrieval.py`: converted discriminative component scores into ranked lists and fused them via `rrf_fuse(k=60)`.
- `memorymaster/retrieval.py`: applied pinned and tier bonuses after fusion, then sorted with existing tie-breakers.

Final branch state keeps only this results document; the regressing code path was not retained.

## Metrics

| Metric | E01 baseline | E02 RRF query_rows | Delta |
|---|---:|---:|---:|
| R@5 | 0.9660 | 0.9200 | -0.0460 |
| R@10 | 0.9840 | 0.9800 | -0.0040 |
| MRR | 0.9021 | 0.7151 | -0.1870 |

Benchmark command:

```powershell
$env:MEMORYMASTER_RRF_QUERY_ROWS='1'; python tests\bench_longmemeval.py --retrieval-only
```

Pytest command:

```powershell
python -m pytest tests/ -q --tb=line -x
```

Pytest result: `2058 passed, 46 skipped, 1 xfailed, 1 warning in 569.87s`.

## Decision

**REVERT**. R@5 regressed by 0.0460, which is below the baseline minus 0.005 threshold.

## What Next

If revisiting RRF, evaluate a candidate-only stream design that excludes non-retrieved zero-score items and calibrates post-fusion tier/pinned bonuses against the smaller RRF score range.
