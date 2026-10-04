# Campaigns A, B, O, P: recall performance

This is a read-only static inspection at HEAD `31c9492`, done 2026-10-04. Nothing in this file was measured. Line numbers refer to that commit.

## A. Recall and SQLite tokenization

**Status: confirmed by code.** The cost exists. Its size in the real hook process model is a **hypothesis**.

**Evidence.**
- `recall_tokenizer.py:182-224`: `_corpus_stats` is `@lru_cache(16)` on `(db_path, generation)`. It opens a read-only connection, scans the `text` of every claim that is not archived or superseded, and tokenizes each one with `_candidate_tokens`.
- `:227-240`: `_alias_set` uses the same cache key and does a full `SELECT alias FROM entity_aliases`.
- `:284-288`: `extract_query_tokens` calls `read_generation(db_path)` on every call (`query_cache.py:85`), and that opens a new connection.
- The cache lives in the process and is keyed by generation. A hook that runs as a fresh process per prompt therefore pays the full scan every time, while the long-lived shared server pays it once per corpus change.

**Hypothesis.** A persisted or incremental df, such as a token-stats table or a file keyed by generation, removes the cold scan from per-process hooks. Today the scan time scales with corpus size, not with the query.

**Metrics.**
- Primary: p50/p95 of `extract_query_tokens` in a cold process, at several corpus sizes.
- Secondary: latency of a warm call, RSS, and connections per call.

**Hard constraints.** The selected tokens stay byte-identical for the same corpus. The status filter stays the same, and invalidation still fires on a generation change.

**Reuse.**
- `tests/test_recall_tokenizer.py` covers correctness.
- `scripts/bench_recall_latency.py` defaults to the live database and calls `init_db()`. Do not run it without an explicit disposable database.
- Missing: a tokenizer micro-benchmark on a synthetic database.

**Cost and priority.** Medium cost, priority 1, because it sits on the path of every prompt.

## B. Context packing

**Status: confirmed by code.**

**Evidence.**
- `context_optimizer.py:381-411`: `pack_context` builds `proposed = [*included, block]` for each candidate and calls `_measured_context(proposed)`.
- `:369-378`: `_measured_context` re-renders until the self-referencing `tokens_used` stabilizes, which takes at least two renders.
- `:360-366`: the JSON path re-serializes every included row on each render.
- Together this is O(n²) blocks multiplied by the number of fixed-point renders.

**Hypothesis.** Incremental token accounting gives identical inclusion decisions at lower cost. That holds exactly only if `estimate_tokens` is additive, and additivity has not been verified.

**Metrics.**
- Primary: `pack_context` time for N = 20, 50 and 200 rows in each of the text, xml and json formats.
- Secondary: allocations.

**Hard constraints.** These must stay identical: the output string, `claims_included`, `tokens_used`, and row order. That applies across all formats and provider profiles. The error for an under-minimum budget stays.

**Reuse.** `tests/test_context_optimizer.py` and `tests/test_context_optimizer_provider.py` serve as golden oracles. `pack_context` is pure, so no database is needed.

**Cost and priority.** Low cost, priority 1. The code is pure and deterministic, and it already has a golden oracle.

## O. Vector scoring and cache

**Status.**
- **Confirmed by code:** vector scoring and `_rehydrate_cached_rows`.
- **Hypothesis:** that the hook skips the query cache.
- **Partially confirmed:** the per-request clock and tokens.

**Evidence.**
- `stores/_storage_lifecycle.py:650-690`, `vector_scores`:
  - Unless the store is `read_only`, it calls `upsert_embeddings`, which is a write on a read path.
  - It fetches embeddings with one `IN (...)` query.
  - For each row it recomputes `embedding_content_hash`, runs `json.loads`, and computes a pure-Python `cosine_similarity`.
- `core/service.py:1634-1656`: `_rehydrate_cached_rows` calls `get_claim(id, include_citations=True)` once per id, which is an N+1 pattern.
- `retrieval.py:160`: `datetime.now` is called once per claim inside `_freshness_score`.
- `context_hook.py:1335` and `:1402` build `MemoryService(read_only=True)`.

**Hypothesis.** Batched hydration plus a cache of decoded vectors cut the cost per candidate.

**Metrics.**
- Primary: time of `vector_scores` and of `_rehydrate_cached_rows` for K candidates.
- Secondary: SQL statements per call.

**Hard constraints.**
- Scores stay equal within float tolerance.
- The `(sim+1)/2` clamp stays, and so do the model and content-hash skips.
- The read-only guard stays, and the read-only path gets no new write.
- The archived and temporal filters, citations, and order all stay.

**Reuse.** `tests/test_vector_search.py`, `test_embedding_efficiency.py`, `test_ro_recall.py`, `test_service_embedding_toctou.py` and `test_r32_query_storage_efficiency.py`. `benchmarks/perf_smoke.py` creates and removes its own temporary database.

**Cost and priority.** Medium cost, priority 2. The payoff depends on whether vectors are enabled live, and that has not been verified.

## P. SQL fan-out and graph expansion

**Status.**
- **Confirmed by code:** the token fan-out and the relaxed probes.
- **Hypothesis:** that graph expansion runs once per frontier node.

**Evidence.**
- `context_hook.py:1445-1457`: one `_governed_prompt_rows` query runs per token, with an early exit at `_fts_cap`. If nothing is found, it falls back to a raw-prompt query.
- `context_hook.py:982-1000`: `_entity_fanout_claim_ids` runs one SELECT per alias. That is intentional: the code comment calls it a per-entity cap.
- `candidate_pool.py:26-46` grows its window geometrically up to a cap. `:98-112` pages `list_claims_page`.
- `_storage_read.py:267-350`: `_fts_relaxed_rows` runs a `LIMIT 1` probe query per term.
- `graph_expansion.py:458-496` runs one query per entity. `_neighbour_steps` (about lines 380-455) runs one query per neighbour kind.

**Hypothesis.** A single FTS query with an OR expression, or a batched `IN`, would reduce the number of statements. **It can change ranking.**

**Metrics.**
- Primary: SQL statements per recall, counted with a sqlite trace callback, plus wall time.
- Secondary: rows fetched.

**Hard constraints.** Ids and order stay the same. The scope, tenant and authorization filters stay. The per-token caps stay, and so does the `relaxed_out` metadata.

**Reuse.**
- `tests/test_recall_delivery_contract.py`, `test_retrieval_profile.py` and `test_e2e_review_contracts.py`.
- `tests/test_recall_latency.py` only asserts that latency log lines are emitted. It does not measure speed.
- Missing: a statement counter.

**Cost and priority.** High cost, priority 3. This campaign carries the highest risk of breaking ranking equivalence.
