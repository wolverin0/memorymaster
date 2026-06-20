# v316-S2 — RRF as tiebreaker for near-tie candidates

## Hypothesis

When two candidates have linear-blend scores within ~0.01 of each other in the top-10, their relative ordering is essentially noise. RRF voting across the 4 component rankings (lexical, vector, confidence, freshness) is a robust tiebreaker that weighs CONSENSUS across signals, leaving clear-winner pairs untouched but reshuffling near-ties.

Predicted Δ R@5: +0.005 to +0.010. Risk: low — base ranking preserved.

## Change

- `memorymaster/config.py` — added `MEMORYMASTER_RRF_TIEBREAKER` flag (default OFF in production), `MEMORYMASTER_RRF_TIEBREAKER_THRESHOLD` (default 0.01)
- `memorymaster/retrieval.py` — `apply_rrf_tiebreaker(ranked, threshold, enabled)` post-pass that walks the linear-blend-sorted list, groups adjacent near-tie candidates within `threshold`, and reorders each group by RRF score over the 4 component rankings. Items outside near-tie groups keep their linear-blend order.
- `tests/test_retrieval_rrf_tiebreaker.py` — 3 unit tests:
  - `test_clear_winner_pair_unchanged` (0.8 vs 0.5 score gap → no swap)
  - `test_near_tie_reordered_by_rrf` (0.81 vs 0.80 → RRF re-ranks based on component consensus)
  - `test_disabled_flag_no_op` (env flag OFF → identity function)

All 3 unit tests pass.

## Results

| Metric | Baseline (v316-S1) | S2 with `RRF_TIEBREAKER=1` | Δ |
|---|---|---|---|
| R@5 | 0.9660 | **0.9660** | 0.000 |
| R@10 | 0.9840 | **0.9840** | 0.000 |
| MRR | 0.9021 | **0.9006** | -0.0015 |

500/500 questions completed. Bench wall-clock: ~45min.

## Verdict: **NULL**

The RRF tiebreaker activated on near-tie pairs but the reshuffles didn't change which session_ids landed in top-5 / top-10. MRR drift is within measurement noise (single-question position changes can move MRR by ~0.001).

## Why this happened

At R@5 = 0.966, the top-5 ranking is already well-determined by the linear blend. Near-tie reshuffles are too rare and too local to materially affect set-membership recall (R@K). MRR could in principle move from same-set rerankings, but only marginally.

## What next

Per the v3.16+ roadmap, the remaining levers are:
- **S3** (per-question-type retrieval profiles) — DEMOTED in mm-3e3d after S1's override-bench showed W_LEX bumps hurt at vector-enabled baseline. May still work with non-W_LEX axes (W_VEC profiles), but predicted upside is small.
- **A1** (full QA pass with judge) — blocked on `ANTHROPIC_API_KEY` not being in the shell env (mm-2c65).
- **A2** (LongMemEval-M) — ~5h codex wall-clock for one positioning number; deferred.

**Recommendation:** ship v3.16.0 with S1 (architectural unblock, KEEP) + S2 (NULL doc) and close the cycle. Re-open when ANTHROPIC_API_KEY is configured for A1 or there's a new direction.
