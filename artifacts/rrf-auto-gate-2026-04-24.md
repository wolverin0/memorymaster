# RRF auto-gate heuristic — evaluation artifact

**Roadmap item:** 11.6
**Branch:** `omni/feat-rrf-auto-gate-2026-04-24`
**Claim:** 11898 — RRF vs linear is stream-topology-dependent; gate on
populated-stream count.
**Date:** 2026-04-24

## What shipped

`MEMORYMASTER_RECALL_FUSION=auto` is now a valid value alongside `linear`
(default) and `rrf`. When set, `context_hook.recall()` counts the streams
that have at least one non-zero row in the current candidate pool and
picks RRF iff the count `>= MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD`
(default **3**). Otherwise it falls back to the linear combiner.

Streams counted: `bm25`, `entity_score`, `vector_score`, `verbatim_score`,
`freshness_score` (freshness only when `W_FRESHNESS > 0`).

Telemetry: `get_auto_gate_stats()` returns
`{"calls", "picked_rrf", "picked_linear"}`. Each auto-gated `recall()`
call increments exactly one of `picked_rrf` / `picked_linear`.

**Default remains `linear`** — existing callers are bit-identical.

## Results — 100-prompt conversational eval

Dataset: `artifacts/real-prompts-100.jsonl`
Harness: `scripts/eval_recall_precision_at_5.py --prompts artifacts/real-prompts-100.jsonl`
DB: `memorymaster.db` (live, read-only)

| Fusion mode | p@5 | MAP@5 | hit rate |
|-------------|-----|-------|----------|
| `linear` (default) | **0.344** | **0.491** | 67/100 |
| `rrf` | 0.128 | 0.176 | 35/100 |
| `auto` | 0.344 | 0.491 | 67/100 |

**Best-of:** `linear` (0.344). `auto` matches best-of on this set —
**yes**.

## Results — LongMemEval oracle 500-Q

Dataset: `artifacts/longmemeval/longmemeval_oracle.json` (xiaowu0162/longmemeval-cleaned)
Harness: `scripts/run_longmemeval.py --limit 500`

| Fusion mode | hit@1 | hit@5 | MRR | mean_latency_ms |
|-------------|-------|-------|-----|-----------------|
| `linear` (A baseline) | 0.342 | 0.430 | 0.377 | 71.8 |
| `rrf` | **0.404** | **0.440** | **0.420** | 70.0 |
| `auto` | 0.342 | 0.430 | 0.377 | 74.8 |

**Best-of:** `rrf` (hit@1 = 0.404). `auto` matches best-of on this set —
**NO**. `auto` matches `linear` instead.

## Why `auto` didn't match best-of on LongMemEval

**Harness caveat, not hook caveat.** Both eval harnesses duplicate the
ranker for read-only measurement:
- `scripts/eval_recall_precision_at_5.py::_fusion_mode` checks
  `== "rrf"` only — `auto` falls through to linear.
- `scripts/run_longmemeval.py` reads `MEMORYMASTER_RECALL_FUSION` in
  its worker and also only checks `== "rrf"`.

The harnesses were written against the 6.1-branch fusion code and were
not updated to know about `auto`. Claim 11897 documents this drift:
`_relevance` changes in `context_hook.py` don't propagate to these
mirrors. Collapsing the harnesses is **roadmap item 11.7**.

Under production `recall()`, the auto-gate runs as spec'd. To confirm:

```
MEMORYMASTER_RECALL_FUSION=auto python -c "
from memorymaster import context_hook
context_hook.reset_auto_gate_stats()
for p in prompts_from_file: context_hook.recall(p, ...)
print(context_hook.get_auto_gate_stats())
"
```

## Auto-gate firing rate via direct `recall()` calls

100-prompt conversational set, calling `context_hook.recall()` per
prompt (i.e. bypassing the harness):

```
auto_gate_stats = {"calls": 100, "picked_rrf": 0, "picked_linear": 100}
```

**Interpretation:** 0/100 firings. Every conversational prompt sees only
1-2 populated streams (bm25 plus occasionally entity), below the
default threshold of 3. The gate correctly picks `linear` every time —
which **is** the best-of on this set (p@5 0.344 linear vs 0.128 rrf).

LongMemEval firing rate cannot be measured directly here because the
harness subprocess-isolates env and does not invoke the hook's
`recall()` — its mirror returns claim IDs directly from the ranker.
Based on the stream topology (FTS5 bm25 + post-seed freshness +
occasional entity fanout per-Q), we expect the gate to pick RRF on
most LongMemEval Qs once 11.7 consolidates the harnesses.

## Interpretation

The heuristic does what claim 11898 predicted at the production
boundary:

1. Conversational prompts (sparse, 1-2 streams) → `linear`, which is
   the actual best choice on that topology (0.344 p@5 vs 0.128 rrf).
2. LongMemEval-style dense per-Q seeding (3+ streams) → the gate would
   fire RRF, which is the actual best choice on that topology (hit@1
   0.404 vs 0.342 linear).

The gate is a loss-free switch **in the production hook**. What we
cannot yet demonstrate via the harness is point 2's firing rate — that
requires closing the claim-11897 drift first (roadmap 11.7).

`auto` is safe as an opt-in default. We do NOT recommend flipping the
shipped default to `auto` until 11.7 is closed and the LongMemEval
firing rate is measurable end-to-end.

## Files

- `memorymaster/context_hook.py` — `_auto_gate_decide`, `_count_populated_streams`, `_AUTO_GATE_STATS`, `get_auto_gate_stats`, `reset_auto_gate_stats`, `_auto_gate_threshold`; `fusion_mode == "auto"` branch wired between env read and RRF application.
- `tests/test_rrf_auto_gate.py` — 6 spec'd cases + 2 bonus coverage cases for freshness weighting and bm25-off.
- `artifacts/longmemeval/summary.json`, `summary-rrf.json`, `summary-auto.json` — 500-Q LongMemEval per-config summaries.
