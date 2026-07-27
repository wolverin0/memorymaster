# LongMemEval-S retrieval results
# Covers: the current reproducible 500-question retrieval benchmark and its limits.
# Key terms: LongMemEval-S, Recall@5, Recall@10, MRR, retrieval-only, QA judge.
# Read when: checking retrieval regressions or reproducing the published local result.
# Source: committed benchmark/longmemeval_s_results.json, verified at d33a268.
# Status: current retrieval evidence; no current comparable full-QA result exists.
# Updated: 2026-07-27 to remove stale and partial QA headline values.

## Current result

The committed LongMemEval-S cleaned result contains all 500 retrieval questions:

| Questions | Recall@5 | Recall@10 | MRR |
| ---: | ---: | ---: | ---: |
| 500 | 0.9660 | 0.9840 | 0.9020579365 |

This is retrieval-only evidence. It is not a full answer-generation or judge
accuracy result.

## Reproduce

```powershell
python tests\bench_longmemeval.py --retrieval-only
```

The harness downloads `longmemeval_s_cleaned.json` into the gitignored
`benchmark/data/` directory and writes results to
`benchmark/longmemeval_s_results.json`. It uses a fresh temporary SQLite
database per question and does not read or write the active MemoryMaster
database.

## Release comparison rule

- Compare Recall@5 and MRR to this result using the same dataset and harness.
- A release candidate may regress by no more than 0.01 absolute.
- Full QA must use the same OAuth-backed judge before and after the change.
- Historical quota-limited partial judge runs are not valid QA baselines and
  are intentionally not presented as accuracy metrics here.

The pre-vNext environment, package, collection, and latency measurements are
recorded in
[`.planning/VNEXT-BASELINE-2026-07-27.md`](../../.planning/VNEXT-BASELINE-2026-07-27.md).
