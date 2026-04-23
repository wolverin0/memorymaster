# Spec — Calibrated classifier for steward promotion (#129)

**Status:** draft, builds on `artifacts/steward-redesign-notes.md`.
**Author:** claude-session, 2026-04-23.
**Measurement baseline:** `scripts/eval_steward_pareto.py` on the 200-row
fixture showed the additive `validation_score` can reach TP=49, FP=1
(49% recall, 99% precision) — but cannot go further by threshold tuning.
That ceiling is the formula's, not the threshold's.

---

## Problem

`validation_score` is an additive hand-tuned sum of:

- `citation_score = min(1.0, n_citations / 3) * 0.3`
- `source_trust = 0.2` if `source_agent in {claude-session, codex-session}` else `0.1`
- `conflict_penalty = -0.15 * n_conflicts`
- `age_bonus = 0.15` if `age > 7 days and accessed`
- ... etc

The fixture sweep shows the frontier at (min_cit=0, min_score=0.74) with
49% recall. Beyond that point, **every increase in threshold drops TPs
and FPs at the same rate** because the score does not separate the two
populations. Adding more features to the additive sum just adds noise;
what we need is a feature-weight combination learned from labels.

## Proposal — logistic regression with scikit-learn

### Features (v1)

| Feature | Source | Rationale |
|---|---|---|
| `n_citations` | `citations` table | Already known proxy. |
| `source_agent_trust` | Learned per-agent prior | Replaces the hardcoded 0.2/0.1 |
| `scope_quality` | Bin of claim scope (global / project:x / project) | `project` fallback is a red flag per audit |
| `conflict_delta` | `n_conflicts` minus `n_related_agreeing` | Net disagreement signal |
| `session_age_days` | `(now - created_at).days` | Promotion should survive time |
| `access_count` | Claim's `access_count` field | Usage is ground truth for value |
| `has_verbatim_excerpt` | Bool: any citation has excerpt | Cheap quality signal |
| `claim_type_bin` | {bug, decision, constraint, other} | Some types are stricter |
| `sensitivity_flagged` | Bool from filter | Hard-block signal |

### Training pipeline

1. **Label source.** Take the current `confirmed` population as positives
   (n≈2,921) and `archived` + `stale` as negatives (n≈7,622). Subsample
   negatives to 3× positives to keep base rate informative.
2. **Split.** 80/20 chronological (not random) — test on the most recent
   2 weeks to simulate deployment.
3. **Model.** `sklearn.linear_model.LogisticRegression(class_weight='balanced')`.
   Calibrated with `CalibratedClassifierCV(method='isotonic', cv=3)` so the
   output is a real probability.
4. **Artifact.** `artifacts/steward-classifier-v1.joblib` with the fitted
   model + a `feature_version` string for rollback safety.

### Deployment

- `memorymaster/steward_classifier.py` — thin wrapper with `predict_proba`.
- `memorymaster/steward.py::_decide_promotion` — replace the additive
  computation with `score = classifier.predict_proba(features)[1]` and
  fall back to the old formula if the artifact is missing
  (`MEMORYMASTER_STEWARD_CLASSIFIER_PATH` env, default
  `artifacts/steward-classifier-v1.joblib`).
- Decision rule: `score >= 0.65 AND n_citations >= 1` → promote.
  Threshold is tuned against the same Pareto fixture to pick the
  (precision >= 0.95, max recall) point.

## Acceptance

1. On the existing fixture: recall ≥ 70% at FPR ≤ 5% (vs. baseline 49% / 1%).
2. On a held-out chronological split: recall within 5 pp of fixture result.
3. No regression in true-negative archive rate beyond 2 pp.
4. Feature ablation report included in `artifacts/steward-classifier-eval-<date>.md`.
5. Rollback: deleting the `.joblib` file must revert cleanly to the
   additive formula with no steward crash.

## Estimate

- Feature extraction + training pipeline: ~1 day.
- Integration + fallback wiring + rollback test: ~0.5 day.
- Evaluation + writeup: ~0.5 day.

## Risks

- **Label leakage.** Some "archived" claims were archived by the #9 scope
  migration, not because they were bad claims. Exclude archives where
  `status_event.reason like 'migration:%'` from training negatives.
- **Cold start.** A fresh DB has no confirmed corpus. The fallback to
  the additive formula must activate when training set is too small
  (`len(positives) < 200`).
- **Feature drift.** Adding features silently would invalidate the model.
  The `feature_version` check in steward.py hard-errors if the artifact
  version doesn't match the extractor version.
