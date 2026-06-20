# Recall architecture (2026-04-23)

This document describes the retrieval stack that `memorymaster/context_hook.py::recall` runs on every user prompt. It is the canonical reference after the 2026-04-22 → 2026-04-23 sweep landed five retrieval streams, an 8-dimensional ranker, and two fusion modes.

## Pipeline overview

```
prompt
  │
  ├─► tokenizer v2 (recall_tokenizer.py)
  │     df=0 penalty + stem/synonym recovery + whitelist
  │     (see docs/adr/2026-04-23-tokenizer-v2-idf-fix.md)
  │
  ├─► FTS5 query (storage.py)
  │     salient-token MATCH, bm25() ORDER BY
  │
  ├─► candidate rows  (one dict per claim, with per-stream scores)
  │
  ├─► stream enrichment (all in context_hook.py)
  │     • BM25 rescorer       → row["bm25_score"]   (subject-weighted 1.0 / text 1.0)
  │     • entity fanout       → row["entity_score"] (via entity_extractor + aliases)
  │     • vector fallback     → row["vector_score"] (Qdrant, when <3 FTS5 hits)
  │     • verbatim (MemPalace)→ row["verbatim_score"] (opt-in via RECALL_VERBATIM)
  │     • freshness           → row["freshness_score"]
  │
  ├─► fusion  (MEMORYMASTER_RECALL_FUSION)
  │     • "linear" (default) — 8-dim weighted sum, _relevance()
  │     • "rrf"              — reciprocal rank fusion across populated streams
  │                            (recall_fusion.py; see docs/adr/rrf null result)
  │
  └─► budget-aware output (claims joined into "# Memory Context" chunk)
```

## Streams — at a glance

| Stream | Env gate | Dim weight | Default | Source file |
|---|---|---|---|---|
| **lexical / BM25 rescorer** | `MEMORYMASTER_LEXICAL_BM25` | `W_LEXICAL` | 0.3 | `context_hook.py` lines ~520-620 |
| **entity fanout** | `MEMORYMASTER_RECALL_W_ENTITY>0` | `W_ENTITY` | 0.0 (off) | `context_hook.py::_entity_fanout_claim_ids` |
| **Qdrant vector fallback** | `QDRANT_URL` + `MEMORYMASTER_RECALL_W_VECTOR>0` | `W_VECTOR` | 0.0 (off) | `context_hook.py::_apply_vector_fallback` |
| **verbatim memories** | `MEMORYMASTER_RECALL_VERBATIM=1` | `W_VERBATIM` | 0.0 (off) | `context_hook.py` + `verbatim_store.py` |
| **freshness** | always | `W_FRESHNESS` | 0.15 | computed from `last_accessed` |

**Always-on text-overlap signals** (computed inside `_relevance`, not a stream per se):

| Signal | Weight | What it measures |
|---|---|---|
| `W_MATCHES` | 0.1 | count of query tokens (>2 chars) appearing in claim text |
| `W_PHRASE` | 0.3 | full query phrase substring match |
| `W_ALL` | 0.2 | ALL query tokens present |
| `W_CONFIDENCE` | 0.05 | `claim.confidence` field |

All weights are overridable via `MEMORYMASTER_RECALL_W_<NAME>` env vars. The in-file defaults are tuned against the 30-prompt eval as of 2026-04-23.

## Fusion modes

**Linear** (`MEMORYMASTER_RECALL_FUSION=linear`, default)

```
score(claim) = matches·W_MATCHES + phrase·W_PHRASE + all·W_ALL
             + lexical·W_LEXICAL + conf·W_CONFIDENCE + freshness·W_FRESHNESS
             + vector·W_VECTOR + entity·W_ENTITY + verbatim·W_VERBATIM
```

Current baseline: **p@5 = 0.313, MAP@5 = 0.473, non_empty = 17/30** (harness-level; see gotcha below).

**RRF** (`MEMORYMASTER_RECALL_FUSION=rrf`)

Per-stream rankings are produced for each populated stream (zero-score streams are skipped), then fused with `score = Σ 1/(k + rank_in_stream)`, k=60.

Current baseline on this stack: **p@5 = 0.127, MAP@5 = 0.159** — net-negative. Root cause: the overlap signals (`matches`/`phrase`/`all`, combined weight 0.8) are query-text features that RRF cannot consume, and only three of five streams are populated on the eval DB. See `artifacts/rrf-fusion-eval-2026-04-23.md` and claim 11881. Revisit once Qdrant vector recall is active on the eval corpus.

## Known gotchas

- **Eval harness does NOT run the BM25 rescorer.** `scripts/eval_recall_precision_at_5.py` scores via the raw FTS5 `lexical_score` from `query_rows`, not via the production BM25 post-rescorer. A/B comparisons are valid when both sides use the same harness, but absolute numbers diverge from production. A dedicated harness is kept at `artifacts/bm25-per-field-eval-harness.py`. See claim 11882.
- **W_LEXICAL was bumped 0.1 → 0.3 on 2026-04-23** after the BM25 rescorer replaced the overlap-based lexical scorer (commit `a315bf5`). The old 0.1 was tuned for a much weaker signal. See claim 11857.
- **Subject-weighted BM25 does not help on the current DB** — many claims have `subject=None` or generic labels, so text bodies carry more discriminative signal. Defaults shipped as W_SUBJECT=1.0 W_TEXT=1.0 (neutral). See claim 11883 and `artifacts/bm25-per-field-eval-2026-04-23.md`.
- **df=0 tokens used to win ranking** (pre-v2 tokenizer) because `log((N+1)/1)+1` peaks at ~10.38. The v2 penalty + whitelist fixed this. See `docs/adr/2026-04-23-tokenizer-v2-idf-fix.md`.
- **Verbatim retrieval underperforms MemPalace's 96.6% LongMemEval** on our 30-prompt set (+0.013 p@5 / +0.047 MAP@5). Our BM25 + entity + tokenizer v2 stack already captures most of the gain MemPalace measures against a plain keyword baseline. See `artifacts/verbatim-recall-eval-2026-04-23.md`.

## Files of interest

| File | Role |
|---|---|
| `memorymaster/context_hook.py` | Orchestrator. `recall()` is the entry point. |
| `memorymaster/recall_tokenizer.py` | v2 salient-token extractor (df=0 penalty + stem recovery) |
| `memorymaster/recall_fusion.py` | RRF fusion (opt-in) |
| `memorymaster/entity_extractor.py` | Layer-1 regex entity extractor (Layer-2 LLM extractor: Wave B 3.1) |
| `memorymaster/verbatim_store.py` | MemPalace-style raw conversation storage |
| `scripts/eval_recall_precision_at_5.py` | p@5 / MAP@5 on 30-prompt eval (FTS5 lexical only — see gotcha) |
| `scripts/eval_recall_quality.py` | non_empty_rate on 30-prompt eval |
| `artifacts/bm25-per-field-eval-harness.py` | Dedicated harness that DOES run the BM25 rescorer |

## Future levers

- **Dense vector stream populated** — RRF should flip from regress to win once `W_VECTOR > 0` with real embeddings on the eval DB.
- **Classifier v3 with wiki-similarity feature** (Wave C 2.1) — will rebalance the steward population and indirectly affect `freshness` distribution.
- **Eval-set expansion 30 → 100 prompts** (Wave B 1.3) — current thresholds are noisy at N=30.
- **Scope-aware ranking bonus** (Wave G 1.2) — fold current-project scope into `_relevance` as a conditional multiplier.

## 6th stream — Kuzu graph traversal (roadmap 11.3, 2026-04-24)

MemoryMaster now has an opt-in **graph retrieval stream** backed by
Kuzu (embedded, file-based — same "no server" story as SQLite +
Qdrant).

**Intent.** Close the multi-hop gap documented in
`artifacts/cognee-assessment-2026-04-24.md`: BM25 / vector / entity-alias
fanout / verbatim / freshness are all *similarity* mechanisms. None
traverse bridge facts (Alice → Atlas → Postgres). The graph stream is
the first MemoryMaster retrieval that follows edges.

**Wiring.** `memorymaster/graph_store.py` exposes a minimal
`open / close / ingest_edges / neighbors / claims_for_entities` API
over a three-table Kuzu schema:

```
NODE TABLE Claim  (id INT64 PRIMARY KEY)
NODE TABLE Entity (id INT64 PRIMARY KEY, kind STRING)
REL  TABLE Mentions (FROM Claim TO Entity, created_at STRING)
```

`scripts/backfill_graph_store.py` reads `claims.entity_id` and
`entity_aliases.original_form` from the SQLite DB and writes edges
into a **separate** Kuzu file (default `~/.memorymaster/graph.kuzu`).
The live SQLite DB is read-only. Re-running the backfill writes zero
new edges (idempotent).

**Recall.** When `MEMORYMASTER_RECALL_GRAPH=1`, `recall()`:

1. Runs the existing L1 entity extractor on the query.
2. Resolves query entities → entity_ids via `entity_aliases`.
3. BFS in Kuzu: `neighbors(entity_ids, max_hops=MAX_HOPS)`.
4. Reverse-lookup: `claims_for_entities(reached, limit=50)`.
5. Annotates each candidate row: `graph_score = 1.0` iff its
   `claim_id` is in the reached set, else 0.0.
6. Contributes `graph_score * W_GRAPH` to the linear combiner, and
   adds a separate per-ranking input to the RRF fuser.

**Defaults.** `W_GRAPH=0.0`, `GRAPH=0`, `MAX_HOPS=2`. Shipped OFF so
the 5-stream stack is bit-identical.

**Defensive contract (claim 11907).** If Kuzu isn't installed, the DB
is missing, or any traversal raises — the stream returns an empty
reached set and `graph_score=0.0` on every row. The recall hook
falls back to the 5-stream stack without error.

**Null-result caveat.** At `W_GRAPH=0.15` on the current 100-prompt
set, the graph stream produces zero p@5 / MAP@5 lift because (a) L1
entity extraction only matches 33/100 prompts and (b) the graph is so
dense around popular entities ("gemini", "claude") that every top-8
FTS5 candidate is already inside the reached cluster, making the
boolean bonus a constant. See
`artifacts/kuzu-graph-stream-2026-04-24.md` for the full diagnosis and
follow-up levers (Layer-2 LLM entity extraction, distance-weighted
graph_score, per-entity edge caps).

## References

- Commits: `bb71944` (tokenizer v2), `159eef7` (BM25 rescorer), `274577d` (Qdrant fallback), `3f1777c` (verbatim), `a315bf5` (W_LEXICAL=0.3), `f425212` (RRF fusion), `98e25ca` (BM25 per-field plumbing)
- ADRs: `docs/adr/2026-04-23-tokenizer-v2-idf-fix.md`, `docs/adr/2026-04-23-steward-v2-classifier.md`
- Claims: 11853, 11855, 11856, 11857, 11870, 11871, 11881, 11882, 11883, 11896, 11899, 11907
- Roadmap: `artifacts/final-roadmap-2026-04-23.md`
