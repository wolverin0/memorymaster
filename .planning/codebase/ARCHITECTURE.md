# ARCHITECTURE.md — System Architecture (v3.28.0)

## Overview
MemoryMaster is a production-grade memory reliability system for AI coding agents: lifecycle-managed
claims with citations, conflict detection, steward governance, multi-stream recall, an LLM wiki
(Obsidian vault), rule-shaped claims, verbatim archive, and MCP integration. ~110 modules in the
`memorymaster/` package plus `jobs/`, `migrations/`, and `connectors/` subpackages.

## Layers

```
+---------------------------------------------------------------------------+
|                              Entry surfaces                                |
|  CLI (cli.py + cli_handlers_*)  |  MCP server (mcp_server.py, FastMCP      |
|  stdio, ~30 tools)  |  Dashboard (dashboard.py + dashboard_auth.py)        |
|  Hooks (context_hook recall/classify, hook_log, setup_hooks installer)     |
|  LLM Steward CLI (llm_steward.py)  |  Webhooks / scheduler / operator      |
+------------------------------------+--------------------------------------+
                                     |
+------------------------------------v--------------------------------------+
|                       MemoryService (service.py)                           |
|  ingest | query/query_rows | query_for_context | run_cycle | dedup |       |
|  compact | pin | recompute_tiers | recall_analysis | redact                |
|  Cross-cutting: access_control (RBAC), security/sensitivity filter,        |
|  llm_budget (per-cycle caps), query_cache, scope_utils, config             |
+--------------+---------------------------------------+---------------------+
               |                                       |
+--------------v--------------+        +---------------v--------------------+
|         Store layer          |        |        Jobs pipeline (jobs/)       |
| SQLiteStore (storage.py +    |        | extractor -> candidate_dedupe ->   |
|  _storage_* mixins, FTS5+WAL)|        | deterministic -> validator ->      |
| PostgresStore | store_factory|        | decay -> compactor (opt) |         |
| verbatim_store | graph_store |        | dedup, staleness, calibration,     |
| migrations/runner (versioned)|        | compact_summaries, daydream_ingest |
+--------------+--------------+        +------------------------------------+
               |
+--------------v-------------------------------------------------------------+
|  Optional backends: Qdrant (qdrant_backend, vector search), embeddings     |
|  (Gemini -> hash fallback), Kuzu (graph_store), llm_provider (Gemini/      |
|  OpenAI/Anthropic/Ollama/claude_cli keyless)                               |
+----------------------------------------------------------------------------+
```

## Claim lifecycle
Statuses (models.py `CLAIM_STATUSES`): `candidate` -> `confirmed` (steward/validator/dedup promote),
`candidate|confirmed` -> `stale` (jobs/decay freshness window), `-> superseded` (auto_resolver /
conflict_resolver set `supersedes_claim_id` + `replaced_by_claim_id` pair), `-> conflicted`
(contradiction_probe / resolvers surface), `-> archived` (compactor, dedup, steward — terminal).
Orthogonal axes: tier (`core`/`working`/`peripheral`, recomputed each cycle), scope
(`project:<slug>` / `user` / `team:<n>` / `global`), bitemporal fields (`event_time`, `valid_from`,
`valid_until`), visibility (sensitivity filter on every ingest path: MCP, dream_bridge, service).

## Steward pipeline
Two cooperating engines:
1. **Deterministic cycle** — `MemoryService.run_cycle(batch_limit=200, policy_mode, policy_limit)`
   runs, inside an `llm_budget.cycle_scope()` (caps on LLM calls/tokens/failures abort cleanly):
   policy revalidation selection -> `extractor` -> `candidate_dedupe` -> `deterministic` validators
   -> `validator` (min_citations/min_score gates) -> `decay` -> optional `compactor`. Since v3.28
   `batch_limit` threads into every job so a backlogged candidate queue actually drains. Ends with
   a Qdrant post-cycle sync.
2. **LLM steward** — `llm_steward.run_steward(limit, scope=None, use_llm_provider)` pulls candidate
   claims (optionally scope-filtered), runs LLM extraction/curation (confirm, archive, rewrite,
   fact triples), with key rotation + 429 cooldowns, WAL + busy_timeout for concurrent writers,
   optional deterministic auto-validate, and a calibrated promotion classifier
   (steward_classifier + steward_features).

## Recall fusion stack (context_hook + retrieval)
Candidate streams (up to 6): **bm25/lexical** (FTS5 via recall_tokenizer), **vector** (Qdrant or
store-level embedding scores), **entity** (entity_extractor/entity_graph link fanout, W_ENTITY),
**verbatim** (verbatim_recall over raw-conversation archive), **freshness** (counted only when
W_FRESHNESS > 0), **graph** (Kuzu graph_store, distance-weighted 1/(1+hops)). Fusion mode via
`MEMORYMASTER_RECALL_FUSION=linear|rrf|auto`: the **auto-gate** counts populated streams (non-zero
score on >=1 row) and picks RRF (`recall_fusion.rrf_fuse`, k=60) when count >= threshold (default 3,
per-query-type overrides via query_classifier), else the weighted-linear ranker (retrieval.py, with
a floor-ratio gate that suppresses freshness/confidence/tier/pin boosts on weak matches). Post-rank:
optional retrieval profiles, LLM rerank (llm_rerank), query_expansion, context_optimizer token
packing, and a generation-tagged result cache (query_cache).

## Knowledge layers above claims
Wiki engine (wiki_engine absorb/cleanup/breakdown; wiki_validate/freshness/similarity/suggest),
vault tooling (vault_linter, vault_bases, vault_exporter, vault_curator, vault_synthesis,
daily_notes), rules (rules.py, rule_miner mining corrections from verbatim, rule_export), Atlas
Inbox (atlas_contract, atlas_claim_extractor, action_extractor/exporters, media_*), and sync
(db_merge bidirectional OpenClaw merge, delta_sync, dream_bridge, snapshot git-backed versioning).
