# Environment variable reference

Complete inventory of `MEMORYMASTER_*` variables referenced in the package,
**generated** by `scripts/gen_env_reference.py` — do not hand-edit; re-run the
script after adding or removing a variable. For what each does, follow the
listed source files (most are read next to a docstring or comment).

### R1.3 Qdrant containment

An environment variable being referenced does not mean it can activate Qdrant
claim/verbatim payload retrieval. `MEMORYMASTER_RECALL_VECTOR_FALLBACK`,
`MEMORYMASTER_QDRANT_URL`, `MEMORYMASTER_QDRANT_COLLECTION`,
`MEMORYMASTER_EMBED_MODEL`, `MEMORYMASTER_RECALL_VECTOR_LIMIT`,
`MEMORYMASTER_RECALL_VECTOR_MIN_CANDIDATES`, and
`MEMORYMASTER_RECALL_VECTOR_SCORE_THRESHOLD` are retained compatibility/tuning
knobs for the disconnected prompt-context fallback; changing them cannot enable
retrieval during R1.3. The non-prefixed `QDRANT_URL`, `QDRANT_COLLECTION`, and
`OLLAMA_URL`, plus `MEMORYMASTER_QDRANT_DRIFT_MAX`, remain active for upsert,
sync, reconcile, count/ID drift checks, orphan cleanup, and other index
maintenance. Local/primary-store hybrid weights such as `MEMORYMASTER_W_VEC`
are separate from Qdrant retrieval.

Total: 134 variables.

| Variable | Referenced in |
|---|---|
| `MEMORYMASTER_ALLOW_SENSITIVE_BYPASS` | `memorymaster/core/security.py` |
| `MEMORYMASTER_API_KEY` | `memorymaster/core/llm_provider.py`, `memorymaster/govern/llm_steward.py` |
| `MEMORYMASTER_API_KEYS` | `memorymaster/core/llm_provider.py`, `memorymaster/govern/jobs/compact_summaries.py`, `memorymaster/govern/llm_steward.py` |
| `MEMORYMASTER_BM25_` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_BM25_B` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_BM25_K1` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_BOOST_FLOOR_RATIO` | `memorymaster/core/config.py` |
| `MEMORYMASTER_CADENCE_HOURS` | `memorymaster/core/config.py` |
| `MEMORYMASTER_CLAUDE_CLI_BIN` | `memorymaster/core/llm_provider.py` |
| `MEMORYMASTER_CLAUDE_CLI_TIMEOUT` | `memorymaster/core/llm_provider.py` |
| `MEMORYMASTER_CONFIG_FILE` | `memorymaster/core/config.py` |
| `MEMORYMASTER_CONFLICT_MARGIN` | `memorymaster/core/config.py` |
| `MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR` | `memorymaster/surfaces/dashboard_auth.py` |
| `MEMORYMASTER_DASHBOARD_TOKEN_VIEWER` | `memorymaster/surfaces/dashboard_auth.py` |
| `MEMORYMASTER_DASHBOARD_UNSAFE_BIND` | `memorymaster/surfaces/dashboard_auth.py` |
| `MEMORYMASTER_DAYDREAM_INGEST_DIR` | `memorymaster/govern/steward.py` |
| `MEMORYMASTER_DAYDREAM_VERBOSE` | `memorymaster/govern/steward.py` |
| `MEMORYMASTER_DB` | `memorymaster/knowledge/wiki_engine.py`, `memorymaster/stores/snapshot.py` |
| `MEMORYMASTER_DB_RETRIES` | `memorymaster/core/retry.py` |
| `MEMORYMASTER_DB_RETRY_BASE` | `memorymaster/core/retry.py` |
| `MEMORYMASTER_DECAY_RATES` | `memorymaster/core/config.py` |
| `MEMORYMASTER_DEDUPE_ENABLED` | `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`, `memorymaster/govern/candidate_dedupe.py` |
| `MEMORYMASTER_DEDUPE_JACCARD_HIGH` | `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`, `memorymaster/govern/candidate_dedupe.py` |
| `MEMORYMASTER_DEDUPE_SHADOW` | `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`, `memorymaster/govern/candidate_dedupe.py` |
| `MEMORYMASTER_DEFAULT_DB` | `memorymaster/config_templates/hooks/memorymaster-dream-sync.py`, `memorymaster/config_templates/hooks/memorymaster-pretooluse-recall.py`, `memorymaster/config_templates/hooks/memorymaster-recall.py`, `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py` (+4 more) |
| `MEMORYMASTER_DEFAULT_PROJECT_SCOPE` | `memorymaster/surfaces/mcp_server.py` |
| `MEMORYMASTER_DISABLE_ST` | `memorymaster/knowledge/wiki_similarity.py` |
| `MEMORYMASTER_EMBED_MODEL` | `memorymaster/recall/qdrant_recall_fallback.py` |
| `MEMORYMASTER_ENCRYPTION_KEY` | `memorymaster/core/security.py` |
| `MEMORYMASTER_ENTITY_FUZZY_RESOLVE` | `memorymaster/knowledge/entity_registry.py` |
| `MEMORYMASTER_ENTITY_LLM` | `memorymaster/knowledge/entity_extractor.py` |
| `MEMORYMASTER_EVERYTHING_ES_PATH` | `memorymaster/bridges/local_search/everything.py` |
| `MEMORYMASTER_EVERYTHING_TIMEOUT` | `memorymaster/bridges/local_search/everything.py` |
| `MEMORYMASTER_FRESHNESS_HALFLIFE` | `memorymaster/core/config.py` |
| `MEMORYMASTER_HEBBIAN_DECAY` | `memorymaster/core/service.py`, `memorymaster/govern/jobs/decay.py` |
| `MEMORYMASTER_HEBBIAN_DECAY_LAMBDA` | `memorymaster/govern/jobs/decay.py` |
| `MEMORYMASTER_INITDB_FASTPATH` | `memorymaster/stores/migrations/__init__.py`, `memorymaster/stores/storage.py` |
| `MEMORYMASTER_INTAKE_DEFAULT_SOURCE_AGENT` | `memorymaster/core/intake_policy.py` |
| `MEMORYMASTER_INTAKE_MAX_PER_STOP` | `memorymaster/core/intake_policy.py` |
| `MEMORYMASTER_INTAKE_QUOTA_EXEMPT_AGENTS` | `memorymaster/core/intake_policy.py` |
| `MEMORYMASTER_INTAKE_QUOTA_PER_AGENT_PER_DAY` | `memorymaster/core/intake_policy.py` |
| `MEMORYMASTER_INTAKE_QUOTA_WINDOW` | `memorymaster/core/intake_policy.py` |
| `MEMORYMASTER_INTAKE_REJECTED_SCOPE_PREFIXES` | `memorymaster/core/intake_policy.py` |
| `MEMORYMASTER_INTAKE_REJECT_SESSION_STATE` | `memorymaster/core/intake_policy.py` |
| `MEMORYMASTER_INTAKE_REQUIRE_SOURCE_AGENT` | `memorymaster/core/intake_policy.py` |
| `MEMORYMASTER_INTEGRITY_DISABLE` | `memorymaster/govern/jobs/integrity.py` |
| `MEMORYMASTER_KEY_FILE` | `memorymaster/core/key_rotator.py` |
| `MEMORYMASTER_LEXICAL_BM25` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_LEXICAL_WEIGHTS` | `memorymaster/core/config.py` |
| `MEMORYMASTER_LLM_API_KEYS` | `memorymaster/core/llm_provider.py` |
| `MEMORYMASTER_LLM_FALLBACK_MODEL` | `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`, `memorymaster/core/llm_provider.py` |
| `MEMORYMASTER_LLM_FALLBACK_PROVIDER` | `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`, `memorymaster/core/llm_provider.py` |
| `MEMORYMASTER_LLM_KEY_COOLDOWN_SECONDS` | `memorymaster/core/llm_provider.py` |
| `MEMORYMASTER_LLM_KEY_ROTATION` | `memorymaster/core/llm_provider.py`, `memorymaster/recall/llm_rerank.py` |
| `MEMORYMASTER_LLM_MODEL` | `memorymaster/bridges/atlas_llm_extractor.py`, `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`, `memorymaster/core/llm_provider.py`, `memorymaster/govern/contradiction_probe.py` (+3 more) |
| `MEMORYMASTER_LLM_PROVIDER` | `memorymaster/config_templates/hooks/memorymaster-auto-ingest.py`, `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`, `memorymaster/core/llm_provider.py`, `memorymaster/govern/auto_resolver.py` (+6 more) |
| `MEMORYMASTER_LLM_RERANK` | `memorymaster/core/config.py` |
| `MEMORYMASTER_LLM_RERANK_MAX_FAILURES` | `memorymaster/recall/llm_rerank.py` |
| `MEMORYMASTER_LLM_RERANK_MIN_INTERVAL_SECONDS` | `memorymaster/recall/llm_rerank.py` |
| `MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE` | `memorymaster/core/llm_budget.py`, `memorymaster/core/service.py`, `memorymaster/govern/steward.py`, `memorymaster/knowledge/rule_miner.py` |
| `MEMORYMASTER_MAX_PROVIDER_FAILURES_PER_CYCLE` | `memorymaster/core/llm_budget.py` |
| `MEMORYMASTER_MAX_TOKENS_PER_CYCLE` | `memorymaster/core/llm_budget.py` |
| `MEMORYMASTER_MCP_ADMIN_MODE` | `memorymaster/surfaces/mcp_path_policy.py`, `memorymaster/surfaces/mcp_server.py` |
| `MEMORYMASTER_MCP_ALLOWED_SCOPES` | `memorymaster/core/access_control.py` |
| `MEMORYMASTER_MCP_ALLOW_SENSITIVE` | `memorymaster/core/access_control.py` |
| `MEMORYMASTER_MCP_AUTH_MODE` | `memorymaster/core/access_control.py`, `memorymaster/surfaces/setup_hooks.py` |
| `MEMORYMASTER_MCP_DB` | `memorymaster/core/access_control.py` |
| `MEMORYMASTER_MCP_DB_ALLOWLIST` | `memorymaster/surfaces/mcp_path_policy.py`, `memorymaster/surfaces/mcp_server.py` |
| `MEMORYMASTER_MCP_PRINCIPAL` | `memorymaster/core/access_control.py` |
| `MEMORYMASTER_MCP_TENANT_ID` | `memorymaster/core/access_control.py` |
| `MEMORYMASTER_MCP_WORKSPACE` | `memorymaster/core/access_control.py` |
| `MEMORYMASTER_MCP_WORKSPACE_ALLOWLIST` | `memorymaster/surfaces/mcp_path_policy.py` |
| `MEMORYMASTER_PATH_ROOTS` | `memorymaster/bridges/local_search/redact.py` |
| `MEMORYMASTER_PINNED_BONUS` | `memorymaster/core/config.py` |
| `MEMORYMASTER_POLICY_MODE` | `memorymaster/core/policy.py` |
| `MEMORYMASTER_PRETOOLUSE_RECALL` | `memorymaster/config_templates/hooks/memorymaster-pretooluse-recall.py`, `memorymaster/surfaces/setup_hooks.py` |
| `MEMORYMASTER_QDRANT_COLLECTION` | `memorymaster/recall/qdrant_recall_fallback.py` |
| `MEMORYMASTER_QDRANT_DRIFT_MAX` | `memorymaster/core/service.py`, `memorymaster/govern/jobs/qdrant_reconcile.py`, `memorymaster/surfaces/cli.py` |
| `MEMORYMASTER_QDRANT_URL` | `memorymaster/recall/qdrant_recall_fallback.py` |
| `MEMORYMASTER_QUARANTINE_DIR` | `memorymaster/govern/jobs/fk_repair.py` |
| `MEMORYMASTER_QUERY_CACHE` | `memorymaster/recall/query_cache.py`, `memorymaster/stores/migrations/0004_query_cache.py` |
| `MEMORYMASTER_QUERY_INCLUDE_LEGACY_PROJECT` | `memorymaster/surfaces/mcp_server.py` |
| `MEMORYMASTER_RECALL_` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD_` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_CLOSETS` | `memorymaster/knowledge/closets.py`, `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_CLOSETS_BOOST_ONLY` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_FUSION` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_GRAPH` | `memorymaster/recall/context_hook.py`, `memorymaster/recall/graph_store.py` |
| `MEMORYMASTER_RECALL_GRAPH_CANDIDATES` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_GRAPH_MAX_HOPS` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_GRAPH_PATH` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_QUERY_EXPANSION` | `memorymaster/recall/context_hook.py`, `memorymaster/recall/query_expansion.py` |
| `MEMORYMASTER_RECALL_SCOPE_BOOST` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_TWO_PASS` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_TWO_PASS_MAX` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_TWO_PASS_USE_EDGES` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_VECTOR_FALLBACK` | `memorymaster/recall/qdrant_recall_fallback.py` |
| `MEMORYMASTER_RECALL_VECTOR_LIMIT` | `memorymaster/recall/qdrant_recall_fallback.py` |
| `MEMORYMASTER_RECALL_VECTOR_MIN_CANDIDATES` | `memorymaster/recall/context_hook.py`, `memorymaster/recall/qdrant_recall_fallback.py` |
| `MEMORYMASTER_RECALL_VECTOR_SCORE_THRESHOLD` | `memorymaster/recall/context_hook.py`, `memorymaster/recall/qdrant_recall_fallback.py` |
| `MEMORYMASTER_RECALL_VERBATIM` | `memorymaster/recall/context_hook.py`, `memorymaster/recall/verbatim_recall.py` |
| `MEMORYMASTER_RECALL_W_ENTITY` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_W_FRESHNESS` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_W_GRAPH` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_W_LEXICAL` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_W_VERBATIM` | `memorymaster/recall/verbatim_recall.py` |
| `MEMORYMASTER_RETRIEVAL_PROFILE_` | `memorymaster/core/config.py` |
| `MEMORYMASTER_RETRIEVAL_WEIGHTS` | `memorymaster/core/config.py` |
| `MEMORYMASTER_RETRIEVAL_WEIGHTS_NO_VECTOR` | `memorymaster/core/config.py` |
| `MEMORYMASTER_ROLES_CONFIG` | `memorymaster/core/access_control.py` |
| `MEMORYMASTER_ROLE_` | `memorymaster/core/access_control.py` |
| `MEMORYMASTER_RRF_TIEBREAKER` | `memorymaster/core/config.py` |
| `MEMORYMASTER_RRF_TIEBREAKER_THRESHOLD` | `memorymaster/core/config.py` |
| `MEMORYMASTER_RULE_CONFIDENCE_BOOTSTRAP` | `memorymaster/knowledge/rule_miner.py` |
| `MEMORYMASTER_SCOPE_DEFAULT` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_SCOPE_DISAMBIGUATE` | `memorymaster/surfaces/mcp_server.py` |
| `MEMORYMASTER_SESSION_DIVERSITY_CAP` | `memorymaster/core/config.py` |
| `MEMORYMASTER_SNAPSHOT_DIR` | `memorymaster/govern/jobs/integrity.py`, `memorymaster/stores/snapshot.py` |
| `MEMORYMASTER_SOURCE_AGENT` | `memorymaster/surfaces/mcp_server.py` |
| `MEMORYMASTER_SPOOL_DIR` | `memorymaster/core/spool.py` |
| `MEMORYMASTER_STALE_THRESHOLD` | `memorymaster/core/config.py` |
| `MEMORYMASTER_STEWARD_CLASSIFIER_ENABLED` | `memorymaster/govern/steward_classifier.py` |
| `MEMORYMASTER_STEWARD_CLASSIFIER_PATH` | `memorymaster/govern/steward_classifier.py` |
| `MEMORYMASTER_STEWARD_FEATURE_CACHE` | `memorymaster/knowledge/wiki_similarity.py` |
| `MEMORYMASTER_STEWARD_RULE_MINING` | `memorymaster/core/service.py` |
| `MEMORYMASTER_STEWARD_RULE_MINING_LIMIT` | `memorymaster/core/service.py` |
| `MEMORYMASTER_VALIDATION_THRESHOLD` | `memorymaster/core/config.py` |
| `MEMORYMASTER_VAULT_DIR` | `memorymaster/knowledge/vault_log.py` |
| `MEMORYMASTER_WAL_DISCIPLINE` | `memorymaster/bridges/dream_bridge.py`, `memorymaster/config_templates/hooks/memorymaster-auto-ingest.py`, `memorymaster/config_templates/hooks/memorymaster-dream-sync.py`, `memorymaster/config_templates/hooks/memorymaster-recall.py` (+7 more) |
| `MEMORYMASTER_WEBHOOK_SECRET` | `memorymaster/core/webhook.py` |
| `MEMORYMASTER_WEBHOOK_TIMEOUT` | `memorymaster/core/webhook.py` |
| `MEMORYMASTER_WEBHOOK_URL` | `memorymaster/core/webhook.py` |
| `MEMORYMASTER_WIKI_AUTOPROMOTE_THRESHOLD` | `memorymaster/core/lifecycle.py` |
| `MEMORYMASTER_WIKI_DIR` | `memorymaster/knowledge/wiki_engine.py` |
| `MEMORYMASTER_WIKI_ROOT` | `memorymaster/knowledge/wiki_similarity.py` |
| `MEMORYMASTER_WORKSPACE` | `memorymaster/surfaces/mcp_server.py`, `memorymaster/surfaces/setup_hooks.py` |
| `MEMORYMASTER_W_CONF` | `memorymaster/core/config.py` |
| `MEMORYMASTER_W_FRESH` | `memorymaster/core/config.py` |
| `MEMORYMASTER_W_LEX` | `memorymaster/core/config.py` |
| `MEMORYMASTER_W_VEC` | `memorymaster/core/config.py` |
