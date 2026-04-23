# Enabling the 2026-04-23 v2 Systems

Three env-gated systems shipped this session. All default to legacy/off
to preserve existing behavior. Flip them on when you want the upgrade.

---

## 1. Calibrated steward classifier (`#129b`)

**What it does:** Replaces the hand-tuned additive `validation_score`
formula with a calibrated logistic-regression classifier over 21
features. Daily-stratified eval ROC-AUC: **0.990**.

**Enable:**

```bash
export MEMORYMASTER_STEWARD_CLASSIFIER_ENABLED=1
export MEMORYMASTER_STEWARD_CLASSIFIER_PATH=artifacts/steward-classifier-v2.joblib
```

Windows / Claude Code hook config — add to
`~/.memorymaster/<env>.env` or the cron wrapper.

**Rollback:** unset either env var. Steward falls back to legacy
additive formula. The artifact file can also just be removed.

**Verify it took effect:**

```bash
python -m memorymaster --db memorymaster.db run-cycle
# Look for classifier-driven decisions in the log — you'll see
# per-claim probabilities instead of additive-score buckets.
```

---

## 2. Proactive cadence-based revalidation (policy env switch)

**What it does:** Flips the default `policy_mode='legacy'` (no-op stub)
to `'cadence'` (real proactive revalidator that scores overdue claims
by age-over-cadence × volatility × status). Previously you had to pass
`--policy-mode cadence` on every CLI invocation.

**Enable:**

```bash
export MEMORYMASTER_POLICY_MODE=cadence
```

**Rollback:** unset the env var, default returns to legacy.

**Verify:**

```bash
MEMORYMASTER_POLICY_MODE=cadence python -m memorymaster --db memorymaster.db run-cycle
# Output should show policy: {mode: 'cadence', considered: N, due: M, selected: K}
# where N > 0 (previously always 0 in legacy mode).
```

---

## 3. Recall ranking env-knobs (`#4`)

**What it does:** `memorymaster/context_hook.py::_relevance` is now
7-dimensional (was 5-dim with `freshness` + `vector` wired in at
weight 0). Operators can tune per-dimension weights via env vars.
Grid search confirmed current defaults are near-optimal; these knobs
are for future iteration, not immediate lift.

**Env vars** (all optional, default to near-optimal shipped values):

```bash
export MEMORYMASTER_RECALL_W_MATCHES=0.3
export MEMORYMASTER_RECALL_W_PHRASE=0.3
export MEMORYMASTER_RECALL_W_ALL=0.2
export MEMORYMASTER_RECALL_W_LEXICAL=0.1
export MEMORYMASTER_RECALL_W_CONFIDENCE=0.1
export MEMORYMASTER_RECALL_W_FRESHNESS=0.0
export MEMORYMASTER_RECALL_W_VECTOR=0.0
```

**Important caveat:** ranking is near-optimal; the real bottleneck is
retrieval recall (6/30 eval prompts get zero candidates). Tuning
these weights without raising retrieval first will yield marginal
gains.

---

## Recommended rollout order

1. **cadence policy mode** first — low risk, purely opt-in, immediately
   visible in `run-cycle` output.
2. **steward classifier** next — higher impact, still reversible.
3. **recall ranking knobs** last — only meaningful if retrieval gets
   fixed (see roadmapres.md for retrieval candidates).

Between each step, run a steward cycle and check `[MemoryMaster] auto-
archived N stale unused claims` / `validator.confirmed: N` output —
those are the live signals.
