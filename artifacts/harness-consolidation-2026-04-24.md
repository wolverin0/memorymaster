# Harness Consolidation Report — roadmap 11.7

**Date:** 2026-04-24
**Branch:** `omni/feat-harness-consolidate-2026-04-24`
**Base:** `main` (`7740763`, post 11.1 LLM fallback merge)
**Claim:** 11897 (problem statement)

## Problem

`scripts/eval_recall_precision_at_5.py` was the canonical p@5 / MAP@5 harness,
but it duplicated the ranker inline:

* Its own `_fetch_candidates()` ran FTS5 directly without calling the
  production `query_rows` → `context_hook.recall()` pipeline.
* Its own `_score()` rebuilt a hand-rolled weighted sum from row dict keys
  that the shipped `_relevance` formula no longer matches (after BM25
  rescorer, scope-boost, query-expansion, and RRF fusion were added).
* `recall()` was **never** invoked.

Net effect: every opt-in ranker feature shipped in Wave G (1.2 scope-boost,
1.5 query-expansion, BM25-rescorer, RRF fusion) passed its unit tests and
showed zero delta on this eval. Agents kept shipping features that
"regressed nothing" because the harness wasn't exercising them.

## Fix

1. **Extended `memorymaster/context_hook.py::recall`** with an opt-in
   `return_ids=False` kwarg. When True, `recall()` returns
   `(markdown: str, ids: list[int])` where `ids` are the claim IDs surfaced
   in bullet order. Default `False` preserves the shipped `str` return
   type — every existing caller (MCP, CLI, hook) is unaffected.

   Implementation detail: `_recall_impl` grew a private `_rendered_ids`
   parameter threaded from the public entry point and appended to during
   the bullet-build loop (and the Qdrant semantic-fallback branch).
   Added ~15 lines total, no refactor of existing logic.

2. **Rewrote `scripts/eval_recall_precision_at_5.py`** to:
   - Drop the inline `_fetch_candidates()` + `_score()` duplicates.
   - Invoke `recall(prompt, return_ids=True)` per prompt.
   - Score top-5 using a two-tier label policy:
     - **Ground-truth**: if `<prompts>-labels.json` exists AND has an
       entry for the prompt's `sha1[:16]`, use its `{sha: [claim_ids]}`
       map.
     - **Heuristic fallback**: token-overlap proxy (`min_overlap=2`),
       identical to the old harness semantic. Used for prompts without
       a ground-truth entry and for whole runs without a side-file.
   - Capture all tracked env vars (BM25, fusion, scope-boost, W_*
     weights) in the JSON summary for reproducibility.
   - Report hit@5 + p@5 + MAP@5 + latency mean/p95.

3. **Deleted `artifacts/scope-queryexp-harness.py`** — the ad-hoc
   companion harness from Wave G is fully subsumed by the consolidated
   script.

4. **Added `tests/test_eval_harness.py`** — 5 integration tests, no mocks
   on `recall()`. Builds a fresh temp DB seeded with 20 claims
   (3 topic clusters + 3 distractors) via `MemoryService.ingest`,
   verifies:
   - `return_ids` is opt-in; default return type unchanged.
   - ID list aligns with rendered bullets.
   - Harness runs end-to-end against the seeded DB.
   - Top-5 ordering is deterministic across consecutive runs.
   - Labels side-file is honoured when present; heuristic fallback
     activates when absent.

## Before / after numbers

Both harnesses ran with `MEMORYMASTER_RECALL_VERBATIM=0` and
`MEMORYMASTER_RECALL_VECTOR_FALLBACK=0` against the live `memorymaster.db`
(7.8 GB, read-only via `_record_accesses` override).

### Baseline (linear fusion, shipped default weights)

| Prompt set | OLD p@5 | OLD MAP@5 | NEW p@5 | NEW MAP@5 | NEW hit@5 | Δ p@5 |
|---|---|---|---|---|---|---|
| `real-prompts.jsonl` (30) | 0.307 | 0.473 | **0.273** | **0.545** | 0.567 | −0.034 |
| `real-prompts-100.jsonl` (100) | 0.344 | 0.496 | **0.192** | **0.360** | 0.390 | −0.152 |

### RRF re-check (claim 11881)

Claim 11881 said RRF was net-negative on the 30-prompt eval. Re-measured
via the new production-integrated harness:

| Prompt set | Linear p@5 | RRF p@5 | Linear MAP@5 | RRF MAP@5 | Verdict |
|---|---|---|---|---|---|
| 30 | 0.273 | **0.247** | 0.545 | **0.394** | RRF still net-negative |
| 100 | 0.192 | **0.174** | 0.360 | **0.284** | RRF still net-negative |

Claim 11881 **reproduces on the new harness** (RRF is still worse than
linear on the 30-prompt set). On the 100-prompt set RRF is also
net-negative — a new datapoint worth a claim.

### Scale re-check (claim 11884)

Claim 11884 reported p@5 jumping 0.313 → 0.358 from 30 → 100 prompts. The
old harness numbers above (0.307 → 0.344) are directionally consistent
with that claim (p@5 rises when scaling the eval). The **new** harness
inverts the direction: **0.273 → 0.192**, i.e. p@5 **drops** when the
label set gets stricter.

Root cause of the inversion: the 100-prompt set comes with a
**ground-truth labels side-file** (`real-prompts-100-labels.json`, 70/100
prompts labelled, `min_overlap=3`) — the new harness consults it, so many
"relevant-looking" surface-overlap hits that the old harness credited no
longer count. The 30-prompt set has no side-file so falls back to
heuristic (`min_overlap=2`), which is more permissive.

**Claim 11884's monotonic p@5-scaling conclusion does NOT reproduce on
the new harness.** The old harness measured "how many top-5 surface over
permissive overlap"; the new harness measures "how many top-5 are in
curated ground-truth". These are different metrics — the old number was
apples-to-oranges across the two prompt sets.

## Why the numbers differ (30-prompt set, no GT labels)

Both harnesses use heuristic labels here, yet **25/30** prompts show a
different top-5 ordering. The production ranker that the new harness now
exercises adds features the old one bypassed:

* **BM25 rescorer** (context_hook `_bm25_enabled`): overwrites the raw
  `row["lexical_score"]` with a per-field BM25 score. Old harness
  scored `lexical * W_LEXICAL` using the pre-BM25 value.
* **Scope-boost**: no-op at default `SCOPE_BOOST=0`, but the code path
  is wired.
* **Query-expansion**: no-op at default `QUERY_EXPANSION=0`.
* **Budget-trimmed output**: the production hook stops at
  `budget=2000 tokens`; the old harness ranked top-5 off the whole
  candidate pool (20 rows) without budget trimming — so at the top-5
  boundary, budget-truncation could evict a row that the old harness
  still had ranked.

### 5 prompts where top-5 differs (30-prompt, linear, heuristic labels)

```
Sample 1: "Dale, hagamoslo... evaluar que archivos/queries/metodos podriamos MEJROAR con el /autoresearch..."
  OLD top-5: [430, 7601, 11746, 11747, 11718]
  NEW top-5: [11718, 430, 11746, 9364, 8916]

Sample 2: "hable con vos en claude chat y me diste esto para hacer: G:\...\new fiber.txt que opinas ?"
  OLD top-5: [8494, 8440, 11697, 11873, 9649]
  NEW top-5: [8440, 10775, 11850, 7563, 11852]

Sample 3: "hay que correr el steward? y tambien, el dashboard, podes correrlo,? tenemos muchos claims para revisar ?"
  OLD top-5: [11759, 11838, 11831, 10451, 10458]
  NEW top-5: [8108, 7021, 10458, 11831, 10451]

Sample 4: "vos tenes que mergearlo... sobre lo del cambio de modelo, PORQUE? gastamos el limite de gemini 3.1 flash lite?"
  OLD top-5: [11884, 8563, 11716, 11699, 10707]
  NEW top-5: [11884, 10707, 10704, 3677, 11716]

Sample 5: "camvio algo de golgle? ñorque teniamls 6 laves nl puede ser que esten todas agitadas..."
  OLD top-5: [11696, 11699, 11692, 8563, 8586]
  NEW top-5: [11696, 11699, 11692, 8586, 9154]
```

The differences are real (not cosmetic): every sample shows at least one
claim dropping out of top-5 and being replaced by a different one.

## What this unlocks

Every ranker-internal change is now visible on the new harness:

* Set `MEMORYMASTER_RECALL_FUSION=rrf` and re-run → numbers change.
  (Claim 11881 reproduced above.)
* Set `MEMORYMASTER_RECALL_SCOPE_BOOST=0.1` → numbers change.
* Set `MEMORYMASTER_RECALL_W_FRESHNESS=0.15` → numbers change.
* Toggle `MEMORYMASTER_LEXICAL_BM25=0` → numbers change.

All prior "opt-in ranker improvement" claims that landed while the old
harness was in place should be re-audited on the new one before being
treated as shipped. Flag for Wave H.

## Artifacts written

All JSONL files under `artifacts/` — the pre-existing baseline JSONLs are
untouched per the rules:

* `artifacts/harness-consolidation-new-30-2026-04-24.jsonl` — new harness,
  30 prompts, linear fusion.
* `artifacts/harness-consolidation-new-30-rrf-2026-04-24.jsonl` — new
  harness, 30 prompts, RRF fusion.
* `artifacts/harness-consolidation-new-100-2026-04-24.jsonl` — new
  harness, 100 prompts, linear fusion.
* `artifacts/harness-consolidation-new-100-rrf-2026-04-24.jsonl` — new
  harness, 100 prompts, RRF fusion.

## Files touched

| File | Change |
|---|---|
| `scripts/eval_recall_precision_at_5.py` | full rewrite (420 → 405 LOC, all new) |
| `memorymaster/context_hook.py` | +18 LOC, opt-in `return_ids` kwarg + `_rendered_ids` parameter |
| `tests/test_eval_harness.py` | new, 220 LOC, 5 integration tests (no mocks) |
| `artifacts/scope-queryexp-harness.py` | **deleted** (subsumed) |
| `artifacts/harness-consolidation-2026-04-24.md` | this report |

## Verification

```
pytest tests/test_eval_harness.py -v          → 5/5 passed
pytest tests/ -q --tb=short                   → 1579 passed, 40 skipped, 1 xfailed (173s)
ruff check scripts/eval_recall_precision_at_5.py
          memorymaster/context_hook.py
          tests/test_eval_harness.py          → clean
```
