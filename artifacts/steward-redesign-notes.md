# Steward/validator promotion — redesign diagnosis (2026-04-22)

## The symptom

Steward hook reports 200 candidates processed, 0 promoted. Audit asked:
"tune thresholds or reorder probes to maximise promote_rate @ FPR ≤ 5%."

## What the sweep actually measured

The claim-promotion gate that matters lives in
`memorymaster/jobs/validator.py::run`, not in `memorymaster/steward.py`. The
`steward.py` module only *re-audits* confirmed/stale/conflicted claims — it
never looks at candidates. So the probe-order sweep the task described would
not, by itself, change promote_rate.

The real gate is two lines:

```python
if citation_count < min_citations or score < min_score:
    # stays candidate (or demoted)
```

Tunable knobs:

* `config.validation_threshold` (default **0.58**) → `min_score`
* `config.conflict_margin` (default 0.08)
* CLI-level `--min-citations` (default **1**)

`validation_score` is a fixed closed-form:

    base=0.35 + min(cc*0.12, 0.4) + min(len(text)/240, 0.15) + 0.10*has_tuple
    blended = 0.75*raw + 0.25*prior_confidence

## Why tuning cannot hit the target on this DB

### 1. The live candidate pool is structurally unpromotable

From `memorymaster.db` (read-only):

| citations per candidate | count | % of 876 |
|---|---|---|
| 0 | 816 | 93.1% |
| 1 | 60 | 6.9% |

With `min_citations=1` the maximum possible promote_rate is **6.9%**, full
stop. The "0 promotions from 200" observation is *entirely explained by the
ingest pipeline emitting candidates without citations*. No amount of threshold
tuning downstream fixes this.

### 2. Even among citation-bearing claims, the score is not discriminative

Ground-truth fixture (100 historical positives, 100 historical rejections,
selection rules in `scripts/eval_steward_pareto.py`):

* Positives (past `candidate -> confirmed`, still confirmed @ conf ≥ 0.75):
  scores cluster **0.73-0.85**.
* Negatives (past `candidate -> {conflicted, archived, superseded}` that
  never reached confirmed): scores cluster **0.69-0.74**.

The two distributions overlap precisely in the 0.72-0.74 band where the
gate sits. Under the current formula, moving the threshold from 0.58 to
0.74 drops promote_rate from 100% to 49% on the labeled set, and gains us
**1% FPR** — the only operating point below the 5% cap.

### 3. The formula ignores the signals that matter

What actually separates positives from negatives in the DB:

| Signal | Positives | Negatives | Used by scorer? |
|---|---|---|---|
| prior_confidence | 0.75-1.0 cluster | 0.79 cluster (set by ingest, not derived) | 25% weight — too low |
| conflict-with-confirmed-tuple at ingest time | rare | common (that's *why* they got conflicted) | not used at score time |
| source_agent / scope quality | high-trust in positives | noisy in negatives | not used |
| freshness gap (created_at → last validator touch) | short | long-idle | not used |
| number of distinct sessions citing the tuple | ≥1 | ≈0 | not used |

The current score is a citation/length/structure composite with a sprinkle
of prior confidence. It essentially measures "does this look like a well-
formed claim" — not "is this likely to be true after review."

## Pareto frontier (from `scripts/eval_steward_pareto.py`)

```
min_citations=1  min_score=0.58   promote=100%  FPR=100%   ← baseline
min_citations=1  min_score=0.72   promote=100%  FPR=90%
min_citations=1  min_score=0.74   promote=49%   FPR=1%     ← recommended
min_citations=0  min_score=0.74   promote=49%   FPR=1%     (equivalent; most negs have 1 citation anyway)
```

Every other grid point is dominated.

## Recommendations (ordered by impact)

### P0 — Unblock candidates at INGEST, not at validator (root cause of 0/200)

The validator is not the bottleneck; the **ingest pipeline emits citation-
less candidates 93% of the time**. Fix upstream:

1. `mcp_server.py::ingest_claim` already forces a fallback citation. Audit
   the dream-seed / auto-ingest paths — they almost certainly skip it. Every
   ingest path must attach at least one non-empty `source` with either a
   `locator` or `excerpt`.
2. Block inserts where `citations=[]` outright, or route them into a holding
   table for enrichment before they land in `claims`.
3. Backfill: 816 existing citation-less candidates will never promote. Either
   enrich them via session-transcript mining or archive them with a
   `reason=no_citation_backfill` audit event.

### P1 — Tactical threshold tune (safe, ~10-line diff)

Raise `validation_threshold` from **0.58 → 0.74** *after* P0 lands. This
trades 51% recall for 98% precision on the labeled distribution. Since the
current pipeline produces many low-confidence self-declared "0.5" candidates
that slip through at 0.58, this is a clear improvement.

Do NOT touch `min_citations` — leave at 1. The 0 vs 1 frontier point is
degenerate (almost every candidate with a hope of promotion already has a
citation), so the extra complexity buys nothing.

### P2 — Replace the scorer (redesign, separate PR)

The additive formula cannot reach the 5% FPR / 80%+ promote rate asked for.
A sensible replacement:

* Logistic regression (or GBM) on features: `citation_count`,
  `len(text)`, `has_tuple`, `prior_confidence`, `source_agent` one-hot,
  `session_count_citing_tuple`, `time_since_ingest`,
  `conflicting_tuple_confidence_delta`, `scope_quality_prior`.
* Train on the same `candidate->confirmed` vs
  `candidate->(conflicted|archived|superseded-without-confirm)` labels, with
  temporal holdout (train before 2026-04-01, test after).
* Deploy behind a flag so we can A/B the calibration curve against the
  closed-form scorer.

### P3 — Add *separate* rejection probes to short-circuit

The current "probe chain" in `steward.py` (filesystem_grep, deterministic_*,
semantic_probe, tool_probe) targets **re-auditing confirmed claims**, not
promotion. If we want those probes in the promotion path, add them as a
*veto* pass before the score gate — not as the main signal:

* `deterministic_citation_locator` missing-path → demote to stale immediately
* `tool_probe` storage mismatch → archive with `reason=ingest_corruption`

That keeps the promotion gate fast (today it's a single arithmetic expression)
while still using the probes we already built.

## What this branch ships

* `scripts/eval_steward_pareto.py` — read-only Pareto harness, reruns in
  seconds, so future parameter sweeps are one command away.
* `tests/fixtures/steward_eval.jsonl` — 100 pos / 100 neg labeled fixture.
* `artifacts/steward-pareto-2026-04-22.md` — sweep results.
* This note — the diagnosis.

**No threshold change in code.** The fixture exposes the hard truth that the
current formula can only hit 5% FPR at 49% promote rate, which is not
acceptable to silently default-on. The P1 bump to 0.74 should ride in a
separate PR after P0 (ingest citation enforcement) makes the metric
meaningful.
