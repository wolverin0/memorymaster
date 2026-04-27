# Recall measurement — top-50 GT (v3.12.0, 2026-04-27)

**Goal:** v3.11.0 hypothesised that the labeled GT (top-15 candidates per prompt) had insufficient coverage to detect lift from new candidates. Test by re-labeling with top-50.

**Result:** top-50 GT raises baseline from **0.104 → 0.470** (+0.366) — the GT-coverage hypothesis was real. **But every v3.9-v3.11 feature is STILL NEGATIVE**: F1 −0.001 to −0.003, F6 boost-only −0.005 to −0.013, F5+F8 −0.033 to −0.058. The features don't aport signal at top-5 on this corpus, regardless of label coverage.

## Method

1. `scripts/precompute_candidates.py --top-k 50` against the 953-prompt set → 10 chunks of 100 prompts × 50 candidates each.
2. 5 parallel haiku subagents labeled. Total: 953 prompts, **646 non-empty (67.8%)**, 2,734 label IDs (avg 4.2/non-empty).
3. Re-ran baseline + F1/F5/F6/F8 sweeps.

## Top-15 vs top-50 GT comparison

| Metric | top-15 GT (v3.10/3.11) | top-50 GT (v3.12) |
|---|---|---|
| Non-empty prompts | 248 (26%) | 646 (67.8%) |
| Total label IDs | ~750 | 2,734 |
| Baseline precision@5 | 0.104 | **0.470** |
| Baseline MAP@5 | 0.184 | 0.568 |
| Baseline hit@5 | 0.235 | 0.667 |

The GT-coverage gap was real and large (+0.366 absolute precision@5).

## Sweep results vs new baseline 0.470

### F1 — claim_type-aware ranking (W_CLAIM_TYPE)

| W | precision@5 | Δ |
|---|---|---|
| 0.0 (off) | 0.470 | — |
| 0.1 | 0.469 | -0.001 |
| 0.3 | 0.468 | -0.002 |
| 0.5 | 0.469 | -0.001 |
| 1.0 | 0.467 | -0.003 |

→ NULL.

### F6 — closets BOOST-ONLY (W_CLOSETS)

| W | precision@5 | Δ |
|---|---|---|
| 0.1 | 0.465 | -0.005 |
| 0.3 | 0.464 | -0.006 |
| 0.5 | 0.461 | -0.009 |
| 1.0 | 0.457 | -0.013 |
| 2.0 | 0.457 | -0.013 |

→ Mild negative.

### F5+F8 — two-pass with shares_entity edges (W_TWO_PASS)

| W | precision@5 | Δ |
|---|---|---|
| 0.1 | 0.437 | -0.033 |
| 0.3 | 0.433 | -0.037 |
| 0.5 | 0.427 | -0.043 |
| 1.0 | 0.412 | -0.058 |

→ Worse than F1/F6. The 2,631 shares_entity edges connect claims aggressively but the resulting neighbors aren't the labelled-correct top-5.

### Combined v3.11 best knobs

| Config | precision@5 | Δ |
|---|---|---|
| W_CLAIM_TYPE=0.5 + W_TWO_PASS=0.3 (edges) + W_CLOSETS=1.0 (boost-only) | 0.421 | -0.049 |

→ Worse than baseline. Stack of small negatives.

## Honest interpretation

The hypothesis that labeled-GT-coverage hid feature lifts was **partially confirmed and partially refuted**:

- **Confirmed**: the v3.6.0 baseline (0.105) was depressed by under-labeling. The TRUE baseline on this corpus is 0.470.
- **Refuted**: the features themselves don't help, even when labels are wider. F1/F5/F6/F8 each move precision@5 in the WRONG direction.

Possible explanations:
1. **The lexical + entity_fanout streams already capture the relevant claims.** When a query like "where is W_LEXICAL defined?" hits, BM25 + entity-resolution already surfaces the canonical claim. F6 closets / F5 two-pass / F8 edges add OTHER claims that mention the same entities but don't answer the query — they displace the right answer from top-5.
2. **The labelled-correct claims are dense in the lexical signal.** When the labeler picked 4-5 IDs from top-50, it tended to pick the same ones the lexical ranker promoted. So adding new candidates can only hurt.
3. **The corpus is lexically-clean.** Real-world recall benefits from fanout when queries use vague terms; our 953 prompts use specific names (`W_LEXICAL`, `claim_type`, `MemPalace`, etc.) that the lexical ranker already nails.

## Decisions

- **Defaults stay at 0.0 / OFF across all v3.9-v3.11 features.**
- **Ship the wider GT** (`artifacts/real-prompts-1000-top50-labels.json`) so future evals have honest baselines.
- **Document the negative result definitively** — three release cycles (v3.10, v3.11, v3.12) all converged on the same conclusion.
- **Mark the recall-feature track as "investigated, no signal" for v3.13+.** Future research time should go to:
  - Real-world recall capture (instrument the recall hook to log query+returned-IDs in a separate corpus)
  - Vector recall (the W_VECTOR weight is at 0.0 because there's no Qdrant index — this is a more promising lever)
  - Compaction/dedup (reduce corpus noise rather than re-rank)

## Reproducibility

```bash
# Pre-compute candidates with top-50 (was top-15)
python scripts/precompute_candidates.py --prompts artifacts/real-prompts-1000.jsonl --db memorymaster.db --out-dir artifacts/label-batches-top50 --chunk-size 100 --top-k 50

# Label via 5 parallel haiku subagents (see git log for the prompt template)

# Eval against the wider GT
python scripts/eval_recall_precision_at_5.py \
  --prompts artifacts/real-prompts-1000-top50.jsonl \
  --db memorymaster.db \
  --json-out /dev/null --label baseline-top50

# Sweep the env-gated streams to reproduce the table above
```
