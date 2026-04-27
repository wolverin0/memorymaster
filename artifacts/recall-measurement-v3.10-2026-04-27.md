# Recall measurement — v3.10.0 (2026-04-27)

**Goal:** measure whether F1/F5/F6/F8 from v3.9.0 lift recall@5 once they're wired into context_hook.recall() and the live tables are populated.

**Result:** F1, F5, F8 are NULL (within ±0.001 noise). F6 is **actively harmful** (-0.018 to -0.044). **Ship no default changes.** Defaults stay at 0.0 across all four new weights.

This is an honest-negative release. The machinery + tests + measurement infrastructure ship; the integration is gated behind opt-in env flags so legacy ranking is bit-identical.

## Setup

- Eval harness: `scripts/eval_recall_precision_at_5.py`
- Prompts: `artifacts/real-prompts-1000.jsonl` (953 prompts, 953 labels, 248 non-empty)
- DB: `memorymaster.db` post-rebuild
- Live populations performed before measurement:
  - `rebuild_edges`: 17,252 claims scanned → **462 edges written** (143 mention + 319 supersession). 2.7% claim coverage.
  - `rebuild_closets`: **620 articles indexed**, 46 skipped (exempt or empty body).

## Baseline

| Metric | Value |
|---|---|
| precision@5 | **0.104** |
| MAP@5 | 0.184 |
| hit@5 | 0.235 |
| latency p95 | 17.5 ms |

## F1 — claim_type-aware ranking (W_CLAIM_TYPE sweep)

| Weight | precision@5 | MAP@5 | Δ |
|---|---|---|---|
| 0.0 (off) | 0.104 | 0.184 | — |
| 0.1 | 0.104 | 0.184 | 0.000 |
| 0.3 | 0.104 | 0.184 | 0.000 |
| 0.5 | 0.104 | 0.184 | 0.000 |
| 1.0 | 0.103 | 0.184 | -0.001 |

→ **NULL.** Possible cause: the `classify_observation` patterns are too coarse — they match `preference` for most queries before reaching `decision`/`constraint`. Re-running with a tighter classifier may unlock signal in v3.11.

## F5 — Two-pass entity-fanout (W_TWO_PASS sweep, GRAPH off, EDGES off)

| Weight | precision@5 | MAP@5 | Δ |
|---|---|---|---|
| 0.0 (off) | 0.104 | 0.184 | — |
| 0.1 | 0.103 | 0.184 | -0.001 |
| 0.3 | 0.103 | 0.184 | -0.001 |
| 0.5 | 0.103 | 0.184 | -0.001 |
| 1.0 | 0.103 | 0.184 | -0.001 |

→ **NULL.** Entity-fanout neighbors are entering the candidate set but not landing in labelled top-5. Same pattern v3.6.0 documented for the GRAPH stream.

## F5 + F8 — Two-pass + claim_edges walker (combined)

| Weight | precision@5 | MAP@5 | Δ |
|---|---|---|---|
| 0.1 | 0.103 | 0.184 | -0.001 |
| 0.3 | 0.103 | 0.184 | -0.001 |
| 0.5 | 0.103 | 0.183 | -0.001 |
| 1.0 | 0.102 | 0.183 | -0.002 |

→ **NULL** (with mild W=1.0 regression). Cause: only 462/17252 = 2.7% of claims have any structural edge. The graph is too sparse to surface enough additional matches into the top-5 even when traversed.

## F6 — Closets (W_CLOSETS sweep) — **HARMFUL**

| Weight | precision@5 | MAP@5 | Δ |
|---|---|---|---|
| 0.1 | **0.086** | 0.163 | **-0.018** |
| 0.3 | **0.080** | 0.162 | **-0.024** |
| 0.5 | **0.074** | 0.154 | **-0.030** |
| 1.0 | **0.060** | 0.123 | **-0.044** |

→ **ACTIVELY HARMFUL.** Why:
- Each closet match returns a wiki article slug + ALL its claim_ids (typically 3-15 claims/article).
- With `closet_score = 1.0` constant, those claims get the same boost as a strong lexical hit.
- 5 closet hits per query × ~10 claim_ids each = ~50 boosted candidates flooding the top-5, displacing the labelled-correct results.

**Fixes for v3.11:**
1. Scale `closet_score` by the FTS5 BM25 score of the closet match (not constant 1.0).
2. Cap closet-hydrated candidates per query (e.g. top-3 articles only).
3. Use closets to BOOST already-recalled claims (intersect with rows), don't add new candidates.

## Combined (F1=0.3 + F5+F8=0.3 + F6=0.1)

| precision@5 | MAP@5 | Δ |
|---|---|---|
| 0.086 | 0.163 | -0.018 |

→ Dominated by F6's negative signal. Combination doesn't recover.

## Decisions for v3.10.0

1. **No default changes shipped.** All four weights stay at 0.0; all three env-gates default to off. Legacy ranking bit-identical.
2. **F6 fix is the highest-priority next-lever.** The constant-1.0 score is a bug hidden by the env-gate — if a user enabled closets in good faith, recall would drop ~17% absolute. Document this STRONGLY.
3. **F8 needs more edges.** 2.7% coverage is too sparse. Future improvement: add `references_topic` edge kind (entity-mediated) so any claim mentioning an entity that another claim also mentions becomes connected. That would push coverage to ~50%+.
4. **F1 needs a tighter classifier.** Current 6-pattern observation classifier is too greedy. v3.11 should use the existing `query_classifier.py` (which is more precise) instead of `classify_observation`.

## Reproducibility

```bash
# Populate
python -c "from memorymaster.claim_edges import rebuild_edges; print(rebuild_edges('memorymaster.db'))"
python -c "from memorymaster.closets import rebuild_closets; print(rebuild_closets('memorymaster.db', 'obsidian-vault/wiki'))"

# Measure baseline
python scripts/eval_recall_precision_at_5.py --prompts artifacts/real-prompts-1000.jsonl --db memorymaster.db --json-out /tmp/x.jsonl --label baseline

# Sweep individual streams via env vars (see CHANGELOG for the full env-var list)
```

## Lessons learned

- Three different attacks on the GRAPH-flat hypothesis (F1, F5, F8) ALL produced null. The hypothesis "more candidates = more signal" appears wrong on this prompt set — adding candidates without a sharp ranking signal just adds noise.
- The one stream that DID move the metric (F6) moved it the wrong direction, because of a scoring bug (constant boost) — not because the underlying idea is wrong. MemPalace's R@1 +38% came from BM25-scored closet pointers, not constant ones.
- Honest negative results are still valuable: this measurement saved us from shipping defaults that would silently hurt users who turned features on. The env-gates are doing their job.
