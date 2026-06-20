# S1 Results: Unify Retrieval Weight Constants

## Hypothesis

Once both paths read identical weights, the v3.15.0-E05 W_LEX sweep can be re-run as a follow-up (not part of THIS experiment). For THIS experiment, just verify that:
- At default weights (matching what's hardcoded today), R@5 stays at 0.9660 +/- 0.001 (no regression - the refactor itself must be neutral)
- The env override `MEMORYMASTER_W_LEX=0.55` (or `MEMORYMASTER_RETRIEVAL_WEIGHTS=lex:0.55,conf:0.25,fresh:0.15,vec:0.05` - whichever format the codebase uses) now CHANGES the bench result. Even if it changes for the WORSE, that proves the threading works.

## What Changed

- `memorymaster/config.py`: changed the canonical vector-enabled retrieval defaults to the previous semantic hardcoded blend: `(lex=0.30, conf=0.20, fresh=0.10, vec=0.40)`.
- `memorymaster/config.py`: added individual env overrides `MEMORYMASTER_W_LEX`, `MEMORYMASTER_W_CONF`, `MEMORYMASTER_W_FRESH`, and `MEMORYMASTER_W_VEC`; these override the comma-separated `MEMORYMASTER_RETRIEVAL_WEIGHTS` values when set.
- `memorymaster/retrieval.py`: changed `_compute_claim_score` so the semantic-vector branch consumes `cfg.retrieval_weights` instead of hardcoded literals. The non-semantic vector-enabled branch already used `cfg.retrieval_weights`, so both vector-enabled ranking paths now share the same source.

## Metrics

| Run | R@5 | R@10 | MRR | Notes |
| --- | ---: | ---: | ---: | --- |
| Baseline v3.15.0-E01-locked | 0.9660 | 0.9840 | 0.9021 | Given baseline |
| S1 default weights | 0.9660 | 0.9840 | 0.9021 | Neutral |
| S1 `MEMORYMASTER_W_LEX=0.55` | 0.9560 | 0.9820 | 0.8976 | Changed, proving env threading reaches semantic ranking |

Default-weight delta: R@5 `+0.0000`, R@10 `+0.0000`, MRR `+0.0000`.

## Regression Test Summary

- Added `tests/test_retrieval_weights.py`.
- The test sets `MEMORYMASTER_W_LEX=0.55`, resets config, and asserts the score delta appears in both vector-enabled paths: `semantic_vectors=False` and `semantic_vectors=True`.
- Focused test result: `1 passed`.
- Full suite result: `2060 passed, 46 skipped, 1 xfailed`.

## Decision

Verdict: KEEP.

Default R@5 stayed at `0.9660`, the new regression test passes, and full pytest is green. The override bench changed metrics, which confirms the sweep knob now reaches the semantic-aware ranking path.

## What Next

S1 unblocks S3 (per-question-type retrieval profiles). Re-run E05 W_LEX sweep with proper threading to confirm before S3.
