# EXPERIMENT v315-E05 - W_LEX sweep

## Hypothesis

The locked E01 baseline used R@5 = 0.9660, R@10 = 0.9840, and MRR = 0.9021. This experiment tested whether increasing the lexical component in the hybrid retrieval ranker improves LongMemEval-S retrieval by giving phrase/token overlap more influence.

Predicted Delta R@5: +0.005 to +0.015.

## Method

The benchmark uses `embedding_provider=all-MiniLM-L6-v2 semantic=True`. That semantic path had a hardcoded blend in `memorymaster/retrieval.py`, so the sweep was run with a temporary local edit that made semantic vector mode read `Config.retrieval_weights`. The edit was reverted after the sweep because all tested settings regressed.

For each run, lexical weight was raised and the offset was taken from confidence. Freshness and vector weights stayed fixed.

Benchmark command pattern:

```powershell
$env:MEMORYMASTER_RETRIEVAL_WEIGHTS='<weights>'; python tests\bench_longmemeval.py --retrieval-only --output <output>
```

## Metrics

| W_LEX | Weights lex,conf,fresh,vec | R@5 | R@10 | MRR | Delta R@5 | Verdict |
|---:|---|---:|---:|---:|---:|---|
| 0.50 | 0.50,0.25,0.15,0.10 | 0.9440 | 0.9720 | 0.8653 | -0.0220 | regress |
| 0.55 | 0.55,0.20,0.15,0.10 | 0.9440 | 0.9700 | 0.8647 | -0.0220 | regress |
| 0.60 | 0.60,0.15,0.15,0.10 | 0.9440 | 0.9700 | 0.8646 | -0.0220 | regress |

Best by R@5: W_LEX = 0.50, R@5 = 0.9440, R@10 = 0.9720, MRR = 0.8653.

## Decision

**REVERT**. All three W_LEX candidates were below the locked R@5 baseline by more than 0.005. The branch keeps no code changes from the sweep.

## Notes

- The apparent lexical bump is harmful under the semantic vector retrieval path.
- The shared failure point suggests the current top-5 plateau is not limited by too little lexical weight.
- If this is revisited, test a smaller vector-to-lex rebalancing separately from confidence reduction.

## Verification

- `python -m pytest tests/ -q --tb=line -x`: 2058 passed, 46 skipped, 1 xfailed, 1 warning in 569.58s.
