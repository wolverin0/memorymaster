# EXPERIMENT v315-E04 - Session-diversity reranker

## Hypothesis

LongMemEval-S answers are distributed across multiple haystack sessions. If ranked results cluster too heavily on one session before the final top-K cutoff, capping results per source session could diversify the returned set and improve R@5.

Predicted Delta R@5: +0.005 to +0.015 over the locked E01 baseline.

## What Changed

Files:

- `memorymaster/retrieval.py`
- `memorymaster/config.py`

Change:

- Added `apply_session_diversity_cap(ranked, cap)`.
- Extracts a session key from `claim.source_agent`, then first citation source, then subject, with claim id fallback.
- Applies the cap after ranking and before the final `limit` slice.
- Added `MEMORYMASTER_SESSION_DIVERSITY_CAP`, default `3`; set to `0` to disable.

## Metrics

Locked E01 baseline:

| Metric | Baseline |
| --- | ---: |
| R@5 | 0.9660 |
| R@10 | 0.9840 |
| MRR | 0.9021 |

E04 retrieval-only run:

| Metric | E04 |
| --- | ---: |
| R@5 | 0.9660 |
| R@10 | 0.9840 |
| MRR | 0.9021 |

Delta:

| Metric | Delta |
| --- | ---: |
| R@5 | +0.0000 |
| R@10 | +0.0000 |
| MRR | +0.0000 |

## Decision

NULL. R@5 did not move relative to the locked E01 baseline, and pytest passed.

## Verification

- `python tests/bench_longmemeval.py --retrieval-only`: completed 500/500; R@5=0.9660, R@10=0.9840, MRR=0.9021.
- `python -m pytest tests/ -q --tb=line -x`: 2058 passed, 46 skipped, 1 xfailed, 1 warning in 571.64s.
- `ruff check memorymaster/retrieval.py memorymaster/config.py`: passed.
- `ruff check memorymaster/`: failed on pre-existing E402 findings in unrelated files (`_storage_schema.py`, hook templates, `transcript_miner.py`, `verbatim_store.py`).

## What Next

Do not spend another experiment on source-session diversity unless the benchmark scorer changes to consume more duplicated chunks. The current LongMemEval-S retrieval path already deduplicates by session before scoring, and the top-30 candidate window appears sufficient for the locked baseline.
