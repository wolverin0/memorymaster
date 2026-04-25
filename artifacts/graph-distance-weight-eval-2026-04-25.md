# Wave §12.1 — Graph Score Distance-Weighting Eval

**Branch:** `omni/feat-graph-distance-weight-2026-04-25`
**Main base:** `fb3d7de`
**Date:** 2026-04-25
**Spec source:** claim 12056 (constant-bonus pathology in Wave 11.3)

## Hypothesis

The Wave 11.3 graph stream shipped honest null (Δp@5 = 0.000) because
`graph_score ∈ {0, 1}` was binary: when a query mentions any popular
entity, the 2-hop BFS reaches ~21% of the graph, and every top-8 FTS5
candidate falls in the reached set. Result: every row gets the same
`+W_GRAPH * 1.0` constant — no re-rank.

Fix: change the score shape to `1 / (1 + hops)`, so closer claims get a
much bigger boost (1.0) than far ones (0.5, 0.333). The hop count comes
from a layer-by-layer BFS that tracks `entity_id → min_hop`.

## Implementation

| File | Change |
|------|--------|
| `memorymaster/graph_store.py` | Added `claims_for_entities_with_distance(entity_ids, max_hops, limit) -> list[(claim_id, hops)]` on `GraphStore` and `_NetworkXGraphStore`. Internal helper `_entity_hop_map` runs the layer-by-layer BFS and tracks `entity_id → min_hop`. Existing `claims_for_entities()` kept for compat. |
| `memorymaster/context_hook.py` | New helper `_graph_reached_claim_distance(query, store) -> dict[claim_id, hops]`. Wiring at the graph-stream call site swaps `1.0` for `1.0 / (1.0 + hops)`. Old boolean helper kept for backward compat (returns `set(distance_map.keys())`). |
| `tests/test_graph_distance.py` | 12 new tests on the Cognee Alice→Atlas→Postgres fixture — direct mention (hop 0 → 1.0), 1-hop bridge (0.5), 2-hop chain (0.333), tie-break (smaller hop wins), max_hops cap, empty inputs, isolated entities, sort order, limit truncation, networkx parity, env-disabled fast path, score formula contract. |

## Eval Results — 100 production prompts

```
$ python scripts/eval_recall_precision_at_5.py --prompts artifacts/real-prompts-100.jsonl
```

| Config | p@5 | MAP@5 | hit@5 | latency p95 | Δp@5 vs base |
|--------|-----|-------|-------|-------------|--------------|
| baseline (graph off)              | **0.194** | 0.359 | 0.390 |  20.9 ms | — |
| graph on, distance, W_GRAPH=0.15  | 0.194     | 0.359 | 0.390 | 139.4 ms | **+0.000** |
| graph on, distance, W_GRAPH=0.30  | 0.194     | 0.359 | 0.390 | 129.2 ms | **+0.000** |
| graph on, distance, W_GRAPH=0.50  | 0.198     | 0.357 | 0.390 | 139.0 ms | +0.004 |
| graph on, distance, W_GRAPH=1.00  | 0.198     | 0.356 | 0.390 | 134.5 ms | +0.004 |

**Verdict: honest null.** p@5 lift fails the ≥ 0.01 acceptance bar across
the W_GRAPH range the spec requested (0.15, 0.30) and only nudges +0.004
even when W_GRAPH is cranked to 1.0. The test suite confirms the math
is correct (`tests/test_graph_distance.py` — 12/12 pass) — the issue is
upstream of scoring.

## Diagnostic — why distance weighting did not move the needle

I instrumented `_graph_reached_claim_distance` against the same
100-prompt set:

```
prompts_with_graph_hits = 15/100      # 85% of prompts get NO graph signal
claim hop distribution: {0: 636, 1: 114, 2: 0}
prompts containing each hop:  {0: 15, 1: 7, 2: 0}
```

Two findings:

1. **Coverage is 15%, not 100%.** Entity extraction on the prompt either
   returns no entities or returns aliases that miss the
   `entity_aliases` lookup. Distance weighting is only meaningful for
   the prompts where the stream actually fires — and 85 of 100 prompts
   never even reach the BFS.

2. **Within the 15 hits, hop-0 dominates.** The `limit=50` cap fills
   up with hop-0 claims long before any hop-1/hop-2 entries are
   considered. `claim_hop_distribution = {0: 636, 1: 114, 2: 0}` shows
   the long tail — and crucially, the top-K candidates from FTS5 also
   skew toward hop-0 (popular entities are popular for a reason). When
   the top-5 FTS5 candidates are ALL hop 0, every one of them gets
   `+W_GRAPH * 1.0` — the same constant-bonus pathology, just on a
   smaller subset.

I cross-checked by diffing the top-5 IDs returned by `recall()` with
graph-on (W_GRAPH=0.15) vs graph-off across all 100 prompts:

```
top5 changed in 0/100 prompts (0 of which had graph hits)
```

Even at W_GRAPH=1.0, only 4/100 prompts had any top-5 reorder.

## Sample prompts where distance weighting *would* re-rank

The expected effect — hop 0 outranking hop 1 outranking hop 2 — is
demonstrated correctly in unit tests on the Cognee fixture:

| Claim | Mentions | hop | graph_score |
|-------|----------|-----|-------------|
| 1 | Alice + Atlas | 0 | 1.000 |
| 4 | Alice + Bob   | 0 | 1.000 |
| 2 | Atlas + Postgres (mentions hop-1 entity Atlas) | 1 | 0.500 |
| 5 | Atlas (only)  | 1 | 0.500 |
| 3 | Postgres (only) | 2 | 0.333 |

Tie-break verified: claim 2 mentions both Atlas (hop 1) and Postgres
(hop 2) — emitted at hop 1 (the smaller). See
`tests/test_graph_distance.py::test_two_hop_postgres_chain`.

The math works. The corpus does not exercise it.

## Next-lever recommendation

The spec offered an honest-null escape: *"L1 entity sparsity is still
the bottleneck — wait for L2 backfill"*. The diagnostic above confirms
this: the bottleneck is not the score shape, it is that 85% of
production prompts never produce a single entity_id the graph can BFS
from. The §12.4 L2 entity backfill (parallel work, do not touch) is the
correct next lever — once entity extraction recall on free-form Spanish
prompts is fixed, the distance-weighted score can actually contribute.

Until then, **leave W_GRAPH=0.0 in production** (the shipped default).
The wiring is correct, defensive, and zero-cost when disabled, but the
upstream signal it depends on is too sparse to deliver lift on its own.

A second-order suggestion: revisit `limit=50` after L2 lands. If hop-0
saturation continues to consume the cap, switching to a *per-hop quota*
(e.g. `top-20 hop-0 + top-15 hop-1 + top-15 hop-2`) would force the
ranker to see the long-tail entries the distance score is designed to
re-order.

## Verification

```
$ pytest tests/test_graph_distance.py tests/test_graph_store.py -v
... 26 passed in 3.56s

$ pytest tests/ -q --tb=short
... 1625 passed, 40 skipped, 1 xfailed in 188.01s

$ ruff check memorymaster/graph_store.py memorymaster/context_hook.py
All checks passed!
```

Backfill used for the eval:
```
$ python scripts/backfill_graph_store.py --db memorymaster.db --graph-path artifacts/graph.kuzu
[backfill] claims=2914 edges_considered=21477 edges_written=21477
```

## Files touched

- `memorymaster/graph_store.py` — `+106` lines (new method on Kuzu + networkx stores)
- `memorymaster/context_hook.py` — `+45 / -19` lines (new distance helper, wiring swap, old helper kept as compat shim)
- `tests/test_graph_distance.py` — `+220` lines (new file, 12 tests)
- `artifacts/graph-distance-weight-eval-2026-04-25.md` — this file

No live `memorymaster.db` was modified. No `run-cycle` was triggered.
No commits or pushes performed (per spec).
