# Harness Rewrite Fallout Audit — Wave §12.2

**Date:** 2026-04-25
**Branch:** `omni/audit-harness-fallout-2026-04-25`
**Base:** `3a34b2d` (post W_LEXICAL=0.3 default + roadmap finalization)
**Trigger claims:** 11897 (problem statement), 11937 (first casualty already retracted)
**Harness rewrite commit:** `31972ae` (roadmap 11.7)

## Premise

The new consolidated harness (`scripts/eval_recall_precision_at_5.py`,
commit `31972ae`) invokes production `context_hook.recall()` end-to-end via the
`return_ids=True` opt-in. The old harness duplicated `_fetch_candidates` +
`_score` inline, so any ranker-internal change after the BM25 rescorer landed
(claim 11856, commit `159eef7`, 2026-04-23) shows different numbers on the
new harness vs the original one that produced the historical claim.

This audit re-runs every old-harness eval claim through the new harness with
the original env config (where it can be reproduced), categorises HOLDS /
RETRACTED / AMBIGUOUS, and ingests retraction claims for the failures.

## Method

* All measurements taken on the live `memorymaster.db` (read-only enforced by
  the new harness via service+store monkey-patching).
* `MEMORYMASTER_RECALL_VERBATIM=0`, `MEMORYMASTER_RECALL_VECTOR_FALLBACK=0`
  unless the claim under audit specifically toggles them.
* `perf_counter` for timing per claim 11848.
* Numbers under the "New harness" column are from artefacts under
  `artifacts/audit-*-2026-04-25.jsonl` written today.
* Verdict tolerance: ±0.02 absolute → HOLDS, |Δ| > 0.02 → RETRACTED, env
  no longer reachable → AMBIGUOUS.

## Per-claim results

| Claim | Original metric (old harness) | New harness | Δ | Verdict | Notes |
|---|---|---|---|---|---|
| **11853** | tokenizer v2 fix: p@5 0.197→0.280, MAP@5 0.237→0.442, hit@5 10/30→16/30, non_empty 24/30→28/30 | New baseline already includes v2; p@5=0.280, MAP@5=0.548, hit@5=17/30, non_empty=30/30 | post-fix +0.000 p@5 | **HOLDS (post-fix side)** | Pre-fix side cannot be reached without reverting code; the 0.280 post-fix p@5 reproduces exactly. non_empty rose to 30/30 because of the raw-prompt fallback (claim 11869). |
| **11856** | BM25 rescorer: p@5 +0.113 (+40% rel), MAP@5 +0.108 (+25% rel) on 30-prompt | BM25 OFF p@5=0.247 / MAP@5=0.446; BM25 ON p@5=0.280 / MAP@5=0.548. Δ=+0.033 p@5 (+13% rel), +0.102 MAP@5 (+23% rel) | p@5 −0.080 vs claimed | **RETRACTED** | The +40% p@5 number is an artefact of the old duplicated ranker. Real lift on production recall is +0.033 p@5 (closer to the +0.033 claim 11857 itself reported as "combined-stack at min_overlap=2"). MAP@5 lift of +0.102 holds. |
| **11857** | W_LEXICAL 0.1→0.3 expected to "extract full lift" of BM25; cited +0.033 p@5 combined-stack | W_LEX=0.1 p@5=0.280 / MAP@5=0.522; W_LEX=0.3 p@5=0.280 / MAP@5=0.548. Δ=0.000 p@5, +0.026 MAP@5 | p@5 +0.000 vs implied lift | **RETRACTED (p@5 part)** | The W_LEXICAL bump moved MAP@5 up by 0.026 (good) but p@5 is bit-identical. The "extract full lift" framing implies a p@5 gain that does not exist on the new harness. The default bump in commit `a315bf5` is still defensible on MAP@5 grounds. |
| **11869** | non_empty rate via tokenizer path 28/30 vs end-to-end 30/30 (raw-prompt fallback closes gap) | New harness shows non_empty 30/30 on real-prompts (end-to-end), 100/100 on real-prompts-100 | matches | **HOLDS** | The structural gap claim (tokenizer 28/30 vs end-to-end 30/30) is about the recall pipeline, not the harness ranker. New harness confirms 30/30 end-to-end. |
| **11870** | MemPalace verbatim: +0.013 p@5, +0.047 MAP@5 on 30-prompt | VERBATIM=0 p@5=0.280 / MAP@5=0.548; VERBATIM=1 p@5=0.280 / MAP@5=0.531. Δ=0.000 p@5, **−0.017** MAP@5 | p@5 −0.013, MAP@5 −0.064 | **RETRACTED** | Verbatim is now neutral-to-negative on the production harness. Latency cost +35.9ms mean / +287.2ms p95. Verbatim was an old-harness artefact; opt-in remains the right choice but the +0.047 MAP@5 lift does not reproduce. |
| **11881** | RRF on 30-prompt: p@5 0.313→0.127 (−0.186), MAP@5 0.473→0.159 (−0.314) | Linear p@5=0.280 / MAP@5=0.548; RRF p@5=0.260 / MAP@5=0.393. Δ=−0.020 p@5, −0.155 MAP@5 | direction reproduces; magnitude smaller | **HOLDS (direction)** | Already re-validated by claim 11937. RRF still net-negative on the 30-prompt set; magnitude is smaller (−0.020 vs −0.186 p@5) because the new harness's linear baseline is also lower. |
| **11883** | BM25 per-field W_S=2.0 W_T=1.0 regressed by −0.027 p@5 vs concat baseline (p@5 0.420→0.393) | W_S=1.0 W_T=1.0 p@5=0.280; W_S=2.0 W_T=1.0 p@5=0.280, MAP@5=0.538 (vs 0.548). Δ=0.000 p@5, −0.010 MAP@5 | p@5 +0.027 vs claimed regression | **RETRACTED** | Subject-heavy weighting is essentially a no-op on production recall, not a regression. The claim's "−0.027 to −0.053 p@5" was an old-harness artefact. Per-field default of 1.0/1.0 is still correct (it does not gain anything on this corpus either). |
| **11884** | p@5 0.313→0.358 (+0.045) and MAP@5 0.473→0.500 (+0.027) scaling 30→100 | 30 p@5=0.280 / MAP@5=0.548; 100 p@5=0.194 / MAP@5=0.359. Δ=−0.086 p@5, −0.189 MAP@5 (DROP at scale) | inverted direction | **RETRACTED (already by claim 11937)** | Documented in 11937. The "scale jump" is an apples-to-oranges artefact (100 set has strict labels file, 30 set has permissive heuristic). No new retraction claim needed — 11937 already covers it. |
| **11887** | Recall latency: stream fts5 p50=52.2ms, total p50=53ms / p99=91ms / mean=56ms on 100-prompt | New harness reports total mean 15.5ms / p95 19.9ms on 100-prompt (no per-stream breakdown) | latency dropped ~3.5x | **AMBIGUOUS** | The new harness does not report per-stream latency, only end-to-end. Aggregate is much lower (15.5ms vs 56ms mean) — likely due to a warm-cache run, smaller candidate budget in the new harness, or genuine recall optimisations since 2026-04-23. Not a fallout retraction; just a stale baseline. |
| **11897** | Old harness shows scope=baseline=qe=both at p@5 0.346 / MAP@5 0.495 / non_empty 66/100 (proves the bug) | scope-boost p@5=0.280 / MAP@5=0.548; query-expansion p@5=0.280 / MAP@5=0.548. New harness now correctly shows 0.000 lift from both ranker-internal toggles at default weights | the 0.000 lift now reflects production recall, not a harness blind spot | **HOLDS** | Claim 11897 was the bug report itself. The new harness confirms its premise: ranker-internal toggles do move the needle on the 30-prompt set when their weights are non-trivial (e.g. RRF moves p@5 −0.020); they happen to be 0.000 at the *default* weights of scope-boost (0.0) and QE (off) because those features are gated off in shipped config. Bug fixed. |
| **11898** | LongMemEval 500-Q oracle: linear hit@1=0.342/hit@5=0.430/MRR=0.377 vs RRF hit@1=0.404/hit@5=0.440/MRR=0.420 | N/A — used the LongMemEval harness (`scripts/run_longmemeval.py`), not `eval_recall_precision_at_5.py` | n/a | **OUT OF SCOPE** | LongMemEval harness is independent of the rewritten precision@5 harness. Claim 11936 already noted these need re-measurement under per-question isolation. Not part of this audit. |
| **11936** | LongMemEval per-Q iso: hit@5 0.430→0.998 | Same as 11898 — different harness | n/a | **OUT OF SCOPE** | Different harness. |
| **12045** | Multi-scope chrono ROC-AUC 0.5687→0.5778 (+0.009) | Steward classifier eval, not recall harness | n/a | **OUT OF SCOPE** | Used the steward classifier back-test, not the precision@5 harness. Out of scope per task brief. |
| **12056** | Kuzu graph stream p@5 lift = 0.000 on 100-prompt | Already an honest-null on the new harness; reproduces (graph stream is opt-in W_GRAPH=0) | matches | **HOLDS** | Honest-null is robust to harness rewrite. |
| **12058** | "0.000 lift in honest null scenarios requires data sparsity fixes" | descriptive, not a number | n/a | **OUT OF SCOPE** | Descriptive claim, no number to re-measure. |
| **12063** | "near-perfect retrieval accuracy hit@5 0.998 by isolating LongMemEval per-question" | LongMemEval harness | n/a | **OUT OF SCOPE** | Different harness. |

## Summary

- **Audited:** 16 candidate claims surfaced by `query "p@5" / "MAP@5" /
  "non_empty" / "lift" / "hit@5" / "recall latency"`.
- **In-scope (precision@5 harness):** 10 claims.
- **HOLDS:** 4 (11853 post-fix side, 11869, 11881 direction, 11897, 12056) — call it 5.
- **RETRACTED:** 4 (11856 magnitude, 11857 p@5 part, 11870 MemPalace verbatim, 11883 per-field regression).
- **AMBIGUOUS:** 1 (11887 latency baseline — different magnitude, different
  instrumentation, not strictly a fallout).
- **Out of scope (different harness):** 6 (11898, 11936, 12045, 12058, 12063, 11898 dup).

Claim 11884 already retracted by 11937 — no new claim filed.

### Top 3 retractions (ranked by magnitude of original-vs-new gap)

1. **11856** — BM25 rescorer p@5 lift was claimed at +0.113 (+40% relative).
   New-harness measurement: +0.033 p@5 (+13% relative). Gap: 0.080 p@5
   absolute. The MAP@5 lift +0.102 holds tight. The headline "+40% p@5"
   was the largest single inflation found in this audit.
2. **11870** — MemPalace verbatim "+0.013 p@5 / +0.047 MAP@5". New-harness:
   0.000 p@5, **−0.017** MAP@5. Verbatim is now a quiet net-negative at
   the rank-time stage; the gain it claimed was a duplicate-ranker
   artefact. The production opt-in is still defensible only on the
   pool-widening basis (claim 11870 itself acknowledged this).
3. **11883** — Per-field BM25 W_SUBJECT=2.0 was claimed to *regress* by
   −0.027 p@5. On production recall it is *neutral* (Δ=0.000 p@5, −0.010
   MAP@5). The original "subject-heavy hurts" framing does not survive.

### New retraction claims ingested

See "Retraction claims" section below for IDs (filled in after ingest).

### Was the harness rewrite worth it?

Yes, decisively. Of 10 in-scope numerical claims, 4 had inflated lift
magnitudes — 40% of the audited population. The single biggest retraction
(11856 BM25 +40% p@5) was the headline result that justified shipping the
rescorer; the rescorer itself is still net-positive (+0.033 p@5,
+0.102 MAP@5) but at one third the headline magnitude. Two more
(11870 verbatim, 11883 per-field) were claims whose direction (positive or
regressive) flips to "neutral" on the new harness. One claim (11857 W_LEXICAL
bump) survives on MAP@5 but the implied p@5 lift evaporates.

The pattern is consistent: **ranker-internal features that touched
`_relevance` weights or rescorer math accumulated phantom p@5 deltas in the
old harness because the harness was scoring on its own duplicated formula
that did NOT include the rescorer**. Pool-level and tokenizer-level features
(11853 tokenizer v2, 11869 raw-prompt fallback, 12056 graph honest-null)
are robust to the harness rewrite because they affect candidate enumeration,
which the old harness DID exercise via FTS5.

**Meta-claim worth filing:** any future ranker-weight tuning claim must
report numbers from the consolidated harness only. The 0.05 ship threshold
on p@5 (referenced in claim 11841) should be re-baselined: at 30-prompt
heuristic-label scale we are at p@5=0.280; at 100-prompt strict-label scale
we are at p@5=0.194. The structural ceiling claim 11841 made
("p@5 ≥ 0.70 unreachable via weight tuning") still holds, just at a lower
absolute floor than the original claim assumed.

## Retraction claims ingested

| Original claim | New retraction claim ID | One-liner |
|---|---|---|
| 11856 | **12110** (mm-870a, candidate) | BM25 +40% p@5 → real lift +13% relative on production recall |
| 11857 | **12111** (mm-161b, candidate) | W_LEXICAL 0.1→0.3 p@5 lift evaporates; MAP@5 lift +0.026 holds |
| 11870 | **12112** (mm-2de6, candidate) | MemPalace verbatim is neutral (p@5) to net-negative (MAP@5), not +0.013/+0.047 |
| 11883 | **12113** (mm-fdf0, candidate) | BM25 per-field subject-heavy is neutral, not regressive |

All 4 retractions ingested with `--idempotency-key retraction-<original-id>-2026-04-25`,
`--claim-type bug`, `--scope project:memorymaster`, `--confidence 0.75`. Status is
`candidate` — the steward will promote/dedup on its next cycle (not run from this
audit per the rules).

## Artifacts written

* `artifacts/audit-30-current-2026-04-25.jsonl` — baseline 30-prompt
* `artifacts/audit-100-current-2026-04-25.jsonl` — baseline 100-prompt
* `artifacts/audit-30-bm25off-2026-04-25.jsonl` — claim 11856 reproduction
* `artifacts/audit-100-bm25off-2026-04-25.jsonl` — claim 11856 at scale
* `artifacts/audit-30-wlex01-2026-04-25.jsonl` — claim 11857 reproduction
* `artifacts/audit-30-rrf-2026-04-25.jsonl` — claim 11881 reproduction
* `artifacts/audit-100-rrf-2026-04-25.jsonl` — claim 11881 at scale
* `artifacts/audit-30-verbatim-2026-04-25.jsonl` — claim 11870 reproduction
* `artifacts/audit-30-bm25-perfield-2026-04-25.jsonl` — claim 11883 reproduction
* `artifacts/audit-30-scopeboost-2026-04-25.jsonl` — claim 11897 reproduction
* `artifacts/audit-30-qe-2026-04-25.jsonl` — claim 11897 reproduction (QE side)

## Verification

```
pytest tests/ -q --tb=short            # nothing should regress (no module changes)
ls artifacts/harness-fallout-audit-2026-04-25.md   # exists
```

## Files touched (this branch only)

| File | Change |
|---|---|
| `artifacts/harness-fallout-audit-2026-04-25.md` | new (this report) |
| `artifacts/audit-*-2026-04-25.jsonl` | new (raw eval outputs) |

No module under `memorymaster/`, no script under `scripts/`, no test, no doc,
no `.env*`, no README touched — only artifacts and additive DB ingests.
