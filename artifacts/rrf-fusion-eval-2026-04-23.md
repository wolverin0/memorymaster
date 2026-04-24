# RRF Fusion Eval — 2026-04-23

Roadmap item **1.1 RRF (Reciprocal Rank Fusion)** — opt-in alternative to
linear weighted ranking in `memorymaster/context_hook.py::recall`.

## Commands

```bash
# Baseline (linear, current default)
MEMORYMASTER_RECALL_FUSION=linear python scripts/eval_recall_precision_at_5.py \
  --prompts artifacts/real-prompts.jsonl --db memorymaster.db

# New path (RRF opt-in)
MEMORYMASTER_RECALL_FUSION=rrf python scripts/eval_recall_precision_at_5.py \
  --prompts artifacts/real-prompts.jsonl --db memorymaster.db
```

## Results (30 real prompts, top-K=20 retrieval → top-5 re-rank)

| Mode | precision@5 | MAP@5 | non_empty_rate (>=1 hit in top-5) |
|------|-------------|-------|-----------------------------------|
| **linear (baseline)** | **0.313** | **0.473** | **17/30 (56.7%)** |
| **rrf** | 0.127 | 0.159 | 10/30 (33.3%) |
| delta | -0.186 | -0.314 | -7 prompts |

Mean candidates/prompt: 18.6 (min=1, max=40). Entity-link fanout ON, vector fallback OFF.

## Active streams (non-empty in >= 1 row)

Both modes are fed from the same candidate pool via
`_fetch_candidates` (FTS5 per-token + entity fanout). The streams that
actually contributed to the RRF fusion on this DB:

- **bm25** / `lexical_score` — populated by FTS5 rank on every row (ACTIVE)
- **freshness_score** — populated for every row (ACTIVE)
- **confidence_score** — populated for every row but NOT a fusion stream per
  spec (it is a per-claim attribute, not a retrieval stream)
- **entity_score** — non-zero only for entity-fanout rows (ACTIVE when fanout fires)
- **vector_score** — all zeroes on this DB (Qdrant off) → SKIPPED by RRF
- **verbatim_score** — all zeroes (`MEMORYMASTER_RECALL_VERBATIM=0`) → SKIPPED

## Sample top-5 comparison — 3 prompts where they disagreed

Disagreement rate: **27/30 prompts (90%)** — unsurprising because RRF
ignores the `matches`/`phrase_bonus`/`all_present` query-text signals that
dominate linear scoring (`w_matches=0.3, w_phrase=0.3, w_all=0.2`).

### Prompt 1
> Dale, hagamoslo. Y con todo este contexto, sabes lo que deberiamos hacer, evaluar que archivos/queries/metodos podriamos...

- **linear** top-5 ids: `[430, 7601, 11746, 11747, 11718]` — labels=`[1,1,0,0,1]` → 3 hits
- **rrf**    top-5 ids: `[11221, 11714, 11720, 11790, 11359]` — labels=`[0,0,0,0,0]` → 0 hits

### Prompt 2
> hable con vos en claude chat y me diste esto para hacer: G:\\..\\new fiber.txt que opinas ?

- **linear** top-5 ids: `[8494, 8440, 11873, 11697, 9649]` — labels=`[0,1,1,1,1]` → 4 hits
- **rrf**    top-5 ids: `[10775, 10730, 10738, 11873, 1051]` — labels=`[0,0,0,1,0]` → 1 hit

### Prompt 4
> vos tenes que mergearlo, yo no voy a revisar nada manualmente, y sobre lo del cambio de modelo, PORQUE?...

- **linear** top-5 ids: `[11730, 11708, 452, 8346, 8563]` — labels=`[1,1,0,0,1]` → 3 hits
- **rrf**    top-5 ids: `[9797, 10707, 10704, 11699, 8549]` — labels=`[0,0,0,0,0]` → 0 hits

## Why RRF underperforms here (honest post-mortem)

1. The *linear* scorer folds in **text-match features** (`matches`,
   `phrase_bonus`, `all_present`) computed at ranking time from the query
   text against each candidate's text. These features dominate weighted
   scoring (combined weight 0.8) and are NOT a retrieval stream — they are
   derived from query+text, not a separately-ranked list. The RRF spec for
   this task only fuses BM25 / entity / vector / verbatim / freshness, so
   RRF loses those signals entirely.
2. On this DB **vector** and **verbatim** streams are inert (all-zero
   `vector_score`/`verbatim_score`). RRF is therefore fusing effectively
   3 streams (bm25 + entity + freshness), and freshness is a poor
   relevance proxy — it promotes recent-but-off-topic claims.
3. **Freshness** is ranked descending with no relevance information, so
   it contributes a roughly uniform `1/(k+rank)` bump that shifts the
   ranking toward "most recent" regardless of the query.

These are the exact conditions under which RRF is weak. RRF shines when
every fused stream is relevance-sorted (e.g. BM25 + dense-vector + cross-
encoder re-ranker all computed per-query). It struggles when some "streams"
are actually relevance-agnostic static attributes (freshness, confidence).

## Acceptance check

- Target: RRF `p@5 >= 0.80` and `MAP@5 >= 0.86` → **FAIL** (p@5=0.127, MAP@5=0.159)
- RRF regresses both metrics significantly.

## Recommendation

**Ship the flag as opt-in with `linear` remaining the default.** The RRF
machinery is cheap, correct, and useful for future experiments where
additional relevance-sorted streams come online (dense vector re-ranker,
cross-encoder, per-stream BM25 per-field). Re-run this eval when:

1. Vector fallback becomes the default (Wave C), or
2. Multiple BM25 per-field streams exist (roadmap 1.4), or
3. A cross-encoder re-ranker is added.

In any of those worlds, fused rankings over multiple relevance-sorted
streams may match or exceed linear weighted scoring. For 2026-04-23, the
honest answer is: **RRF is a no-op win today, ship as flag only**.

## Artifacts

- Implementation: `memorymaster/recall_fusion.py` (dataclass + `rrf_fuse`)
- Wiring: `memorymaster/context_hook.py` (env-gated on `MEMORYMASTER_RECALL_FUSION=rrf`)
- Tests: `tests/test_recall_fusion.py` (9 unit tests — all pass)
- Eval harness change: `scripts/eval_recall_precision_at_5.py` reads
  `MEMORYMASTER_RECALL_FUSION` env var and switches `_rank()` accordingly.
