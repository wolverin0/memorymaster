# Recall weight tuning — autoresearch B1 (2026-04-26)

**Verdict:** No default change shipped. Lift ceiling on this eval set is ~+0.002 absolute precision@5, within measurement noise.

## Setup

- Eval harness: `scripts/eval_recall_precision_at_5.py`
- Prompt set: `artifacts/real-prompts-100.jsonl` (100 prompts, 70 labeled, 30 fall-back min_overlap=2)
- DB snapshot: post-L2 entity backfill (entities 16,619 → 24,848 = +49.5%)
- Baseline (current defaults `W_LEXICAL=0.3, W_FRESHNESS=0.0, W_VECTOR=0.0, W_GRAPH=0.0`): **precision@5 = 0.152, MAP@5 = 0.306, hit@5 = 0.350, p95 latency 20.7 ms**

## Sweep #1 — Stream-isolated

`MEMORYMASTER_RECALL_FRESHNESS=1` and `MEMORYMASTER_RECALL_GRAPH=1` set when the corresponding weight > 0.

### W_LEXICAL alone

| W | precision@5 | MAP@5 |
|---|---|---|
| 0.0 | 0.126 | 0.263 |
| 0.05 | 0.148 | 0.297 |
| **0.1** | **0.154** | 0.295 |
| 0.5 | 0.154 | 0.295 |
| 1.0 | 0.154 | 0.292 |
| 2.0 | 0.154 | 0.287 |

→ Saturates at W ≥ 0.1; values above contribute nothing to precision@5 and slightly hurt MAP@5.

### W_FRESHNESS alone

| W | precision@5 |
|---|---|
| 0.0 | 0.126 |
| 0.1 | 0.128 |
| 0.5 | 0.122 |
| 1.0 | 0.114 |
| 2.0 | 0.110 |

→ Peaks at W = 0.1 (+0.002 over no-stream), monotonically degrades after.

### W_GRAPH alone

| W | precision@5 |
|---|---|
| 0.0 | 0.126 |
| 0.1 | 0.126 |
| 0.5 | 0.126 |
| 1.0 | 0.126 |
| 2.0 | 0.126 |

→ **Flat across all weights.** The graph stream contributes zero information on top of the no-LEXICAL baseline. Likely cause: the L2 entity backfill enriched per-claim entity counts, but the graph-traversal stream's `claims_for_entities_with_distance()` path is not surfacing claims that match the labeled ground-truth IDs for these prompts. The +8,229 entities improved coverage of the registry but did not improve recall on this set.

## Sweep #2 — LEXICAL = 0.1 + others

| W_LEXICAL | W_FRESHNESS | W_GRAPH | precision@5 |
|---|---|---|---|
| 0.1 | 0.0 | 0.0 | **0.154** |
| 0.1 | 0.1 | 0.0 | 0.152 |
| 0.1 | 0.5 | 0.0 | 0.146 |
| 0.1 | 0.0 | 0.5 | 0.154 |
| 0.1 | 0.5 | 0.5 | 0.146 |
| 0.1 | 1.0 | 0.5 | 0.144 |
| 0.1 | 0.5 | 1.0 | 0.146 |

→ Adding FRESHNESS or GRAPH on top of LEXICAL = 0.1 is at best neutral (W_GRAPH = 0.5 stays at 0.154 — graph swaps some IDs but neither added nor removed labeled hits) and often hurts (FRESHNESS ≥ 0.1).

## Sweep #3 — Initial 36-cell grid (W_LEX × W_FRESH × W_GRAPH)

All 36 cells produced precision@5 = 0.152 (same as baseline). Cells with non-zero W_FRESHNESS / W_GRAPH **did** swap IDs (verified by diffing `returned_ids` between cells), but the swaps were neutral (replacing a wrong ID with another wrong ID, or a right ID with another right ID). Raw run logs in `artifacts/grid-runs/`.

## Findings

1. **Default `W_LEXICAL = 0.3` is fine.** It produces 0.152 vs the optimal 0.154 — a difference of one labeled hit out of 500 returned items across 100 prompts, well within label-ambiguity noise.
2. **The L2 entity backfill did not lift recall on this eval set.** The graph stream is flat. This does not mean the backfill is worthless — it likely helps elsewhere (entity-level dedup, conflict detection, `find_related_claims` MCP calls) — but the recall hook's labeled prompts don't reward it.
3. **W_FRESHNESS hurts above 0.1.** Recommendation: keep default at 0.0; if turned on, cap at 0.1.
4. **W_LEXICAL is saturated above 0.1.** No precision@5 lift from any value 0.1 ≤ W ≤ 2.0 — the BM25 stream's top-N is rank-stable above a threshold.

## Decisions

- **Ship no weight default changes** in v3.5.2. The empirical optimum is within measurement noise of the current defaults.
- **Document in CHANGELOG / handbook** that:
  - The graph stream (`MEMORYMASTER_RECALL_GRAPH=1`) is opt-in and currently does not improve precision@5 on the held-out 100-prompt set despite the +8,229-entity backfill.
  - The freshness stream peaks at W = 0.1 and should not be raised above that without re-measuring.
  - W_LEXICAL ≥ 0.1 is sufficient; higher values are wasted.
- **Next-lever (deferred):** the graph stream is producing candidates but they're not landing in the labeled top-5. Worth investigating whether the `1.0 / (1 + hops)` distance weighting is the wrong shape, or whether the labeled GT itself is too lexical-biased to reward graph traversal.

## Reproducibility

```bash
# Re-run the full 36-cell grid (~3 min wall):
python scripts/grid_recall_weights.py \
  --prompts artifacts/real-prompts-100.jsonl \
  --db memorymaster.db \
  --output artifacts/recall-weight-tuning-2026-04-26.md
```

Raw per-cell JSONL: `artifacts/grid-runs/`. Combined JSON summary: `artifacts/recall-weight-tuning-2026-04-26.json`.
