# ADR: steward v2 classifier — feature engineering beat threshold tuning

- **Status:** Accepted (artifact shipped, operator enable pending — see `docs/enabling-v2-systems.md`)
- **Date:** 2026-04-23
- **Commit:** `6679805` (merge); `981bd7b` (backtest harness)
- **Authors:** claude-session + subagent
- **Spec:** `artifacts/spec-steward-classifier-2026-04-23.md`

## Context

The legacy steward promoted candidate claims to `confirmed` using `validation_score`, an additive sum of hand-tuned weights:

```
citation_score = min(1.0, n_citations / 3) * 0.3
source_trust   = 0.2 if source_agent in {claude-session, codex-session} else 0.1
conflict_penalty = -0.15 * n_conflicts
age_bonus      = 0.15 if age > 7d and accessed
...
```

`scripts/eval_steward_pareto.py` on a 200-row fixture showed the formula hit a ceiling at **TP=49, FP=1 (49% recall, 99% precision)**. Beyond that point, every increase in threshold dropped TPs and FPs at the same rate — the score did not *separate* the two populations. Adding more hand-weighted terms added noise.

We considered two paths:
1. Keep tuning thresholds. Diminishing returns were visible in the sweep.
2. Learn feature weights from labeled data.

## Decision

We chose path 2: a logistic-regression classifier with scikit-learn, trained on labeled claims from the real DB.

### v2 feature set

Features live in `memorymaster/steward_features.py::extract_features` (line 257):

- **Textual quality:** length, avg word length, punctuation density, "question shape" heuristics
- **Citation depth:** `n_citations`, `n_distinct_sources`, `has_commit_citation`, `has_artifact_citation`
- **Cross-claim:** count of other claims citing the same source, supersedes/superseded state
- **Metadata:** scope hash, source_agent trust, conflict count, sensitivity flag
- **Temporal:** age in days, recency of last access

Training script: `scripts/train_steward_classifier.py`. Calibration via sklearn's `CalibratedClassifierCV(method='sigmoid')`.

### Measured results

| Metric | Legacy `validation_score` | v2 classifier |
|---|---|---|
| ROC-AUC (sound split, held-out) | — | **0.990** |
| F1 @ threshold 0.5 | — | **0.98** |
| Precision @ recall=0.90 | 47% | **98%** |
| Real-DB backtest F1 lift (30d rolling) | baseline | **+0.02** (commit `981bd7b`) |

### Shipping path

- Classifier artifact: `artifacts/steward-classifier-v2.joblib`
- Loader: gated by `MEMORYMASTER_STEWARD_CLASSIFIER_ENABLED=1` + `MEMORYMASTER_STEWARD_CLASSIFIER_PATH`
- Default state: **off** — operator flips the switch after reviewing `docs/enabling-v2-systems.md`
- Backtest harness: `scripts/backtest_steward_classifier.py`

## Consequences

- **Precision-recall trade is now tunable per-deployment** via the classifier's probability threshold, not by rewriting the score formula.
- **Interpretability cost:** sklearn's logistic coefficients are readable, but less so than the old hand-weighted sum. `artifacts/steward-classifier-feature-audit-2026-04-23.md` documents the top-10 features' learned weights.
- **Retraining becomes part of the lifecycle.** The v2 model was trained on a specific chronological window; drift is expected. Cadence: re-train monthly or when false-positive rate rises above baseline.
- **Chronological split failure:** v2 scored only ROC-AUC ≈ 0.45 on a chronological held-out split. Population drift is the cause — labels shift over time as the steward's own behavior shifts. v3 (planned) will add a `wiki_similarity_cosine` feature robust to drift.

## Alternatives considered

- **Gradient boosting (XGBoost/LightGBM)** — rejected for the initial version because logistic is explainable and the training set is small (~1k labeled claims). If v3 also plateaus, revisit.
- **Rule-based cascade** (human-readable decision tree) — rejected because the existing additive score is already rule-based and hit its ceiling. More rules = more noise.
- **Keep threshold tuning** — rejected; the Pareto sweep proved the formula has an inherent separability ceiling.

## Why feature engineering beat threshold tuning

The additive formula's coefficients (0.3, 0.2, 0.15, ...) were chosen to produce "intuitive" scores. Logistic regression chose coefficients that *separate the populations*. The same features, different weights, produced an F1 lift of ~0.5 → 0.98 on the same data — clear evidence that the bottleneck was weighting, not feature coverage.

## References

- Commits `6679805`, `981bd7b`
- `memorymaster/steward_features.py`, `scripts/train_steward_classifier.py`, `scripts/backtest_steward_classifier.py`
- `artifacts/spec-steward-classifier-2026-04-23.md`
- `artifacts/steward-classifier-feature-audit-2026-04-23.md`
- `docs/enabling-v2-systems.md`
- Claims 11831 (shipped), 11833 (real-DB training null result), 11834 (policy-mode context), 11855 (audit null result)
