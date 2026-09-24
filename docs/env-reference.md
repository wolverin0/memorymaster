# Environment variable reference

Complete inventory of `MEMORYMASTER_*` variables referenced in the package,
**generated** by `scripts/gen_env_reference.py` — do not hand-edit; re-run the
script after adding or removing a variable. For what each does, follow the
listed source files (most are read next to a docstring or comment).

Total: 271 variables.

| Variable | Referenced in |
|---|---|
| `MEMORYMASTER_AGY_COMMAND` | `memorymaster/core/antigravity_client.py`, `memorymaster/surfaces/setup_hooks.py` |
| `MEMORYMASTER_AGY_MODEL` | `memorymaster/core/antigravity_client.py` |
| `MEMORYMASTER_AGY_TIMEOUT` | `memorymaster/core/antigravity_client.py` |
| `MEMORYMASTER_ALLOW_SENSITIVE_BYPASS` | `memorymaster/core/security.py` |
| `MEMORYMASTER_ALLOW_SYNTHETIC_MEDIA` | `memorymaster/bridges/evidence_policy.py` |
| `MEMORYMASTER_API_KEY` | `memorymaster/core/llm_provider.py`, `memorymaster/govern/llm_steward.py`, `memorymaster/surfaces/setup_hooks.py` |
| `MEMORYMASTER_API_KEYS` | `memorymaster/core/llm_provider.py`, `memorymaster/govern/jobs/compact_summaries.py`, `memorymaster/govern/llm_steward.py` |
| `MEMORYMASTER_BACKUP_KEY` | `memorymaster/surfaces/operations.py` |
| `MEMORYMASTER_BM25_` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_BM25_B` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_BM25_K1` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_BOOST_FLOOR_RATIO` | `memorymaster/core/config.py` |
| `MEMORYMASTER_CADENCE_HOURS` | `memorymaster/core/config.py` |
| `MEMORYMASTER_CAPTURE_GLOBAL_DAILY_CALLS` | `memorymaster/core/capture_control.py` |
| `MEMORYMASTER_CAPTURE_LLM_MODEL` | `memorymaster/capture/worker.py` |
| `MEMORYMASTER_CAPTURE_LLM_PROVIDER` | `memorymaster/capture/worker.py` |
| `MEMORYMASTER_CAPTURE_LLM_REASONING_EFFORT` | `memorymaster/capture/worker.py` |
| `MEMORYMASTER_CAPTURE_PROVIDER_DAILY_CALLS` | `memorymaster/core/capture_control.py` |
| `MEMORYMASTER_CAPTURE_ROOTS` | `memorymaster/capture/adapters.py`, `memorymaster/public/demo.py` |
| `MEMORYMASTER_CAPTURE_SESSION_DAILY_CALLS` | `memorymaster/core/capture_control.py` |
| `MEMORYMASTER_CAPTURE_STATE_DB` | `memorymaster/core/capture_control.py` |
| `MEMORYMASTER_CAPTURE_TRUST_MODE` | `memorymaster/capture/adapters.py`, `memorymaster/public/demo.py` |
| `MEMORYMASTER_CHECKPOINT_LOG` | `memorymaster/operations/operational_review.py` |
| `MEMORYMASTER_CLAUDE_CLI_BIN` | `memorymaster/core/llm_provider.py` |
| `MEMORYMASTER_CLAUDE_CLI_CWD` | `memorymaster/core/llm_provider.py` |
| `MEMORYMASTER_CLAUDE_CLI_TIMEOUT` | `memorymaster/core/llm_provider.py` |
| `MEMORYMASTER_COMPILED_PROFILE` | `memorymaster/operations/operational_review.py`, `memorymaster/surfaces/scheduled_task.py` |
| `MEMORYMASTER_CONFIG_FILE` | `memorymaster/core/config.py` |
| `MEMORYMASTER_CONFLICT_MARGIN` | `memorymaster/core/config.py` |
| `MEMORYMASTER_DASHBOARD_ALLOWED_ORIGINS` | `memorymaster/surfaces/dashboard_auth.py`, `memorymaster/surfaces/dashboard_origins.py` |
| `MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR` | `memorymaster/surfaces/dashboard_auth.py` |
| `MEMORYMASTER_DASHBOARD_TOKEN_VIEWER` | `memorymaster/surfaces/dashboard_auth.py` |
| `MEMORYMASTER_DASHBOARD_UNSAFE_BIND` | `memorymaster/surfaces/dashboard_auth.py` |
| `MEMORYMASTER_DAYDREAM_INGEST_DIR` | `memorymaster/govern/steward.py` |
| `MEMORYMASTER_DAYDREAM_VERBOSE` | `memorymaster/govern/steward.py` |
| `MEMORYMASTER_DB` | `memorymaster/knowledge/wiki_engine.py`, `memorymaster/public/v1.py`, `memorymaster/stores/snapshot.py` |
| `MEMORYMASTER_DB_RETRIES` | `memorymaster/core/retry.py` |
| `MEMORYMASTER_DB_RETRY_BASE` | `memorymaster/core/retry.py` |
| `MEMORYMASTER_DECAY_RATES` | `memorymaster/core/config.py` |
| `MEMORYMASTER_DECISIONS_DB` | `memorymaster/decisions/archive_gate.py`, `memorymaster/decisions/config.py`, `memorymaster/dreaming/held.py`, `memorymaster/operations/operational_review.py` |
| `MEMORYMASTER_DECISIONS_HOOK_BUSY_MS` | `memorymaster/decisions/config.py`, `memorymaster/decisions/engine.py` |
| `MEMORYMASTER_DECISIONS_LOG_OFF` | `memorymaster/config_templates/hooks/memorymaster-classify.py`, `memorymaster/decisions/config.py`, `memorymaster/decisions/engine.py`, `memorymaster/knowledge/jev_selector.py` |
| `MEMORYMASTER_DECISIONS_STATE_RETENTION_DAYS` | `memorymaster/decisions/config.py`, `memorymaster/surfaces/cli_handlers_jev.py` |
| `MEMORYMASTER_DEDUPE_ENABLED` | `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`, `memorymaster/govern/candidate_dedupe.py` |
| `MEMORYMASTER_DEDUPE_JACCARD_HIGH` | `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`, `memorymaster/govern/candidate_dedupe.py` |
| `MEMORYMASTER_DEDUPE_SHADOW` | `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`, `memorymaster/govern/candidate_dedupe.py` |
| `MEMORYMASTER_DEFAULT_DB` | `memorymaster/config_templates/hooks/memorymaster-dream-sync.py`, `memorymaster/config_templates/hooks/memorymaster-pretooluse-recall.py`, `memorymaster/config_templates/hooks/memorymaster-recall.py`, `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py` (+6 more) |
| `MEMORYMASTER_DEFAULT_PROJECT_SCOPE` | `memorymaster/surfaces/mcp_server.py` |
| `MEMORYMASTER_DISABLE_ST` | `memorymaster/knowledge/wiki_similarity.py` |
| `MEMORYMASTER_DREAM_CAPTURE_MAX_BYTES` | `memorymaster/dreaming/worker.py` |
| `MEMORYMASTER_DREAM_CAPTURE_RETAIN_DAYS` | `memorymaster/dreaming/worker.py` |
| `MEMORYMASTER_DREAM_CONSOLIDATE_MODEL` | `memorymaster/dreaming/providers.py`, `memorymaster/surfaces/scheduled_task.py`, `memorymaster/surfaces/setup_hooks.py` |
| `MEMORYMASTER_DREAM_CONSOLIDATE_PROVIDER` | `memorymaster/dreaming/providers.py` |
| `MEMORYMASTER_DREAM_CONSOLIDATE_VARIANT` | `memorymaster/surfaces/scheduled_task.py`, `memorymaster/surfaces/setup_hooks.py` |
| `MEMORYMASTER_DREAM_ENABLED` | `memorymaster/config_templates/hooks/memorymaster-dream-capture.py` |
| `MEMORYMASTER_DREAM_EXTRACT_MODEL` | `memorymaster/capture/worker.py`, `memorymaster/dreaming/providers.py`, `memorymaster/surfaces/scheduled_task.py`, `memorymaster/surfaces/setup_hooks.py` |
| `MEMORYMASTER_DREAM_EXTRACT_PROVIDER` | `memorymaster/capture/worker.py`, `memorymaster/dreaming/providers.py`, `memorymaster/surfaces/scheduled_task.py`, `memorymaster/surfaces/setup_hooks.py` |
| `MEMORYMASTER_DREAM_EXTRACT_VARIANT` | `memorymaster/capture/worker.py`, `memorymaster/dreaming/providers.py`, `memorymaster/surfaces/scheduled_task.py`, `memorymaster/surfaces/setup_hooks.py` |
| `MEMORYMASTER_DREAM_HELD_RETAIN_DAYS` | `memorymaster/dreaming/held.py` |
| `MEMORYMASTER_DREAM_IDLE_MINUTES` | `memorymaster/dreaming/worker.py` |
| `MEMORYMASTER_DREAM_LEASE_TTL_SECONDS` | `memorymaster/dreaming/worker.py` |
| `MEMORYMASTER_DREAM_MAX_CANDIDATE_WRITES_DAILY` | `memorymaster/dreaming/worker.py` |
| `MEMORYMASTER_DREAM_MAX_CAPTURE_ERRORS` | `memorymaster/dreaming/worker.py` |
| `MEMORYMASTER_DREAM_MAX_CONSOLIDATE_CALLS_DAILY` | `memorymaster/dreaming/worker.py` |
| `MEMORYMASTER_DREAM_MAX_CONSOLIDATE_CANDIDATES` | `memorymaster/dreaming/worker.py` |
| `MEMORYMASTER_DREAM_MAX_CONTEXT_CHARS` | `memorymaster/dreaming/worker.py` |
| `MEMORYMASTER_DREAM_MAX_EXTRACT_CALLS_DAILY` | `memorymaster/dreaming/worker.py` |
| `MEMORYMASTER_DREAM_MAX_INPUT_CHARS` | `memorymaster/dreaming/worker.py` |
| `MEMORYMASTER_DREAM_MAX_INPUT_TOKENS_DAILY` | `memorymaster/dreaming/worker.py` |
| `MEMORYMASTER_DREAM_MAX_SEMANTIC_ATTEMPTS` | `memorymaster/dreaming/worker.py` |
| `MEMORYMASTER_DREAM_MAX_SESSIONS` | `memorymaster/dreaming/worker.py` |
| `MEMORYMASTER_EMBEDDING_PROVIDER` | `memorymaster/recall/embeddings.py` |
| `MEMORYMASTER_EMBED_MODEL` | `memorymaster/recall/qdrant_recall_fallback.py` |
| `MEMORYMASTER_ENCRYPTION_KEY` | `memorymaster/core/security.py` |
| `MEMORYMASTER_ENTITY_FUZZY_RESOLVE` | `memorymaster/knowledge/entity_registry.py` |
| `MEMORYMASTER_ENTITY_LLM` | `memorymaster/knowledge/entity_extractor.py` |
| `MEMORYMASTER_ERROR_TRACKING_DSN` | `memorymaster/govern/operational_health.py` |
| `MEMORYMASTER_EVERYTHING_ES_PATH` | `memorymaster/bridges/local_search/everything.py`, `memorymaster/surfaces/setup_hooks.py` |
| `MEMORYMASTER_EVERYTHING_TIMEOUT` | `memorymaster/bridges/local_search/everything.py` |
| `MEMORYMASTER_FRESHNESS_HALFLIFE` | `memorymaster/core/config.py` |
| `MEMORYMASTER_GRAPH_OBSERVATIONS` | `memorymaster/dreaming/worker.py`, `memorymaster/operations/operational_review.py` |
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
| `MEMORYMASTER_JEV_` | `memorymaster/config_templates/hooks/memorymaster-recall.py`, `memorymaster/decisions/config.py`, `memorymaster/recall/jev_surfaces.py` |
| `MEMORYMASTER_JEV_BATCH_DEADLINE_MS` | `memorymaster/decisions/config.py` |
| `MEMORYMASTER_JEV_BREAKER_PROBE_S` | `memorymaster/decisions/config.py`, `memorymaster/decisions/engine.py` |
| `MEMORYMASTER_JEV_DAILY_USD_CAP` | `memorymaster/decisions/config.py`, `memorymaster/surfaces/cli_handlers_jev.py` |
| `MEMORYMASTER_JEV_DEDUP` | `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`, `memorymaster/govern/candidate_dedupe.py` |
| `MEMORYMASTER_JEV_DEDUP_PER_CYCLE` | `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`, `memorymaster/govern/candidate_dedupe.py` |
| `MEMORYMASTER_JEV_EXPLORE_` | `memorymaster/decisions/config.py` |
| `MEMORYMASTER_JEV_HINTS` | `memorymaster/config_templates/hooks/memorymaster-classify.py` |
| `MEMORYMASTER_JEV_HOOK_DEADLINE_MS` | `memorymaster/decisions/config.py` |
| `MEMORYMASTER_JEV_MODE` | `memorymaster/config_templates/hooks/memorymaster-auto-ingest.py`, `memorymaster/config_templates/hooks/memorymaster-classify.py`, `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`, `memorymaster/decisions/__init__.py` (+4 more) |
| `MEMORYMASTER_JEV_RECALL` | `memorymaster/config_templates/hooks/memorymaster-auto-ingest.py`, `memorymaster/recall/context_hook.py`, `memorymaster/surfaces/mcp_server.py` |
| `MEMORYMASTER_JEV_REVALIDATE` | `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py` |
| `MEMORYMASTER_JEV_REVALIDATE_PER_CYCLE` | `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`, `memorymaster/govern/jobs/revalidation.py` |
| `MEMORYMASTER_JEV_RPM_CAP` | `memorymaster/decisions/config.py` |
| `MEMORYMASTER_JEV_SESSION` | `memorymaster/config_templates/hooks/memorymaster-auto-ingest.py` |
| `MEMORYMASTER_JEV_SKILLS` | `memorymaster/knowledge/jev_selector.py` |
| `MEMORYMASTER_JEV_SKILLS_ENABLED` | `memorymaster/knowledge/jev_selector.py` |
| `MEMORYMASTER_KEY_FILE` | `memorymaster/core/key_rotator.py` |
| `MEMORYMASTER_LEXICAL_BM25` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_LEXICAL_WEIGHTS` | `memorymaster/core/config.py` |
| `MEMORYMASTER_LLM_API_KEYS` | `memorymaster/core/llm_provider.py`, `memorymaster/surfaces/setup_hooks.py` |
| `MEMORYMASTER_LLM_FALLBACK_MODEL` | `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`, `memorymaster/core/llm_provider.py` |
| `MEMORYMASTER_LLM_FALLBACK_PROVIDER` | `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`, `memorymaster/core/llm_provider.py`, `memorymaster/core/provider_health.py`, `memorymaster/surfaces/setup_hooks.py` |
| `MEMORYMASTER_LLM_KEY_COOLDOWN_SECONDS` | `memorymaster/core/llm_provider.py` |
| `MEMORYMASTER_LLM_KEY_ROTATION` | `memorymaster/core/llm_provider.py`, `memorymaster/recall/llm_rerank.py` |
| `MEMORYMASTER_LLM_MODEL` | `memorymaster/bridges/atlas_llm_extractor.py`, `memorymaster/capture/worker.py`, `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`, `memorymaster/core/llm_provider.py` (+6 more) |
| `MEMORYMASTER_LLM_PROVIDER` | `memorymaster/capture/worker.py`, `memorymaster/config_templates/hooks/memorymaster-auto-ingest.py`, `memorymaster/config_templates/hooks/memorymaster-session-end.py`, `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py` (+11 more) |
| `MEMORYMASTER_LLM_REASONING_EFFORT` | `memorymaster/capture/worker.py`, `memorymaster/core/llm_provider.py` |
| `MEMORYMASTER_LLM_RERANK` | `memorymaster/core/config.py` |
| `MEMORYMASTER_LLM_RERANK_MAX_FAILURES` | `memorymaster/recall/llm_rerank.py` |
| `MEMORYMASTER_LLM_RERANK_MIN_INTERVAL_SECONDS` | `memorymaster/recall/llm_rerank.py` |
| `MEMORYMASTER_MANAGED_RECALL_V1` | `memorymaster/config_templates/hooks/memorymaster-recall.py` |
| `MEMORYMASTER_MAX_EMBEDDING_ITEMS_PER_DAY` | `memorymaster/core/usage_ledger.py` |
| `MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE` | `memorymaster/core/llm_budget.py`, `memorymaster/core/service.py`, `memorymaster/govern/steward.py`, `memorymaster/knowledge/rule_miner.py` |
| `MEMORYMASTER_MAX_LLM_CALLS_PER_DAY` | `memorymaster/core/usage_ledger.py` |
| `MEMORYMASTER_MAX_MCP_INGESTS_PER_AGENT_PER_DAY` | `memorymaster/core/usage_ledger.py` |
| `MEMORYMASTER_MAX_MCP_INGESTS_PER_DAY` | `memorymaster/core/usage_ledger.py` |
| `MEMORYMASTER_MAX_PROVIDER_CALLS_PER_DAY` | `memorymaster/core/usage_ledger.py` |
| `MEMORYMASTER_MAX_PROVIDER_FAILURES_PER_CYCLE` | `memorymaster/core/llm_budget.py` |
| `MEMORYMASTER_MAX_TOKENS_PER_CYCLE` | `memorymaster/core/llm_budget.py` |
| `MEMORYMASTER_MCP_ADMIN_MODE` | `memorymaster/surfaces/mcp_path_policy.py`, `memorymaster/surfaces/mcp_server.py` |
| `MEMORYMASTER_MCP_ALLOWED_SCOPES` | `memorymaster/core/access_control.py` |
| `MEMORYMASTER_MCP_ALLOW_SENSITIVE` | `memorymaster/core/access_control.py` |
| `MEMORYMASTER_MCP_AUTH_MODE` | `memorymaster/core/access_control.py`, `memorymaster/surfaces/setup_hooks.py` |
| `MEMORYMASTER_MCP_DB` | `memorymaster/core/access_control.py` |
| `MEMORYMASTER_MCP_DB_ALLOWLIST` | `memorymaster/surfaces/mcp_path_policy.py`, `memorymaster/surfaces/mcp_server.py` |
| `MEMORYMASTER_MCP_HTTP_ALLOWED_HOSTS` | `memorymaster/surfaces/mcp_http.py` |
| `MEMORYMASTER_MCP_HTTP_TOKEN` | `memorymaster/surfaces/mcp_http.py` |
| `MEMORYMASTER_MCP_PRINCIPAL` | `memorymaster/core/access_control.py` |
| `MEMORYMASTER_MCP_TENANT_ID` | `memorymaster/core/access_control.py` |
| `MEMORYMASTER_MCP_TOOL_PROFILE` | `memorymaster/surfaces/mcp_server.py` |
| `MEMORYMASTER_MCP_WORKSPACE` | `memorymaster/core/access_control.py` |
| `MEMORYMASTER_MCP_WORKSPACE_ALLOWLIST` | `memorymaster/surfaces/mcp_path_policy.py` |
| `MEMORYMASTER_MEDIA_MODE` | `memorymaster/bridges/evidence_policy.py` |
| `MEMORYMASTER_OCR_PROVIDER` | `memorymaster/capture/worker.py`, `memorymaster/surfaces/setup_hooks.py` |
| `MEMORYMASTER_ONTOLOGY_FILE` | `memorymaster/knowledge/ontology.py` |
| `MEMORYMASTER_OPENCODE_AUTH_MODE` | `memorymaster/dreaming/providers.py` |
| `MEMORYMASTER_OPENCODE_COMMAND` | `memorymaster/core/llm_provider.py`, `memorymaster/core/opencode_client.py`, `memorymaster/dreaming/providers.py`, `memorymaster/surfaces/setup_hooks.py` |
| `MEMORYMASTER_OPENCODE_TIMEOUT` | `memorymaster/core/llm_provider.py` |
| `MEMORYMASTER_OTEL_ENDPOINT` | `memorymaster/govern/operational_health.py` |
| `MEMORYMASTER_PATH_ROOTS` | `memorymaster/bridges/local_search/redact.py` |
| `MEMORYMASTER_PINNED_BONUS` | `memorymaster/core/config.py` |
| `MEMORYMASTER_POLICY_MODE` | `memorymaster/core/policy.py` |
| `MEMORYMASTER_PRECOMPACT_BLOCKING` | `memorymaster/config_templates/hooks/memorymaster-precompact.py` |
| `MEMORYMASTER_PRETOOLUSE_RECALL` | `memorymaster/config_templates/hooks/memorymaster-pretooluse-recall.py`, `memorymaster/surfaces/setup_hooks.py` |
| `MEMORYMASTER_PROFILE_CADENCE_DAYS` | `memorymaster/profile/engine.py` |
| `MEMORYMASTER_PROFILE_MAP_MODEL` | `memorymaster/profile/providers.py` |
| `MEMORYMASTER_PROFILE_MAX_FACTS` | `memorymaster/profile/engine.py` |
| `MEMORYMASTER_PROFILE_MAX_INPUT_CHARS` | `memorymaster/core/antigravity_client.py`, `memorymaster/profile/engine.py` |
| `MEMORYMASTER_PROFILE_MAX_MAP_CALLS` | `memorymaster/profile/engine.py` |
| `MEMORYMASTER_PROFILE_MAX_MESSAGES` | `memorymaster/profile/engine.py` |
| `MEMORYMASTER_PROFILE_MIN_SESSIONS` | `memorymaster/profile/engine.py` |
| `MEMORYMASTER_PROFILE_OUTPUT_DIR` | `memorymaster/profile/engine.py` |
| `MEMORYMASTER_PROFILE_PREFERENCE_TTL_DAYS` | `memorymaster/profile/engine.py` |
| `MEMORYMASTER_PROFILE_PROVIDER_TIMEOUT` | `memorymaster/profile/providers.py` |
| `MEMORYMASTER_PROFILE_REDUCE_BATCH` | `memorymaster/profile/engine.py` |
| `MEMORYMASTER_PROFILE_REDUCE_MODEL` | `memorymaster/profile/providers.py` |
| `MEMORYMASTER_PROFILE_TOKEN_BUDGET` | `memorymaster/profile/engine.py` |
| `MEMORYMASTER_PROVIDER_FAILURE_EVENT_INTERVAL_SECONDS` | `memorymaster/core/provider_health.py` |
| `MEMORYMASTER_QDRANT_DRIFT_MAX` | `memorymaster/core/service.py`, `memorymaster/govern/jobs/qdrant_reconcile.py`, `memorymaster/surfaces/cli.py` |
| `MEMORYMASTER_QDRANT_GOVERNED_READS` | `memorymaster/surfaces/mcp_server.py` |
| `MEMORYMASTER_QDRANT_OUTBOX_DIR` | `memorymaster/recall/qdrant_outbox.py` |
| `MEMORYMASTER_QDRANT_URL` | `memorymaster/recall/qdrant_recall_fallback.py` |
| `MEMORYMASTER_QDRANT_WRITES` | `memorymaster/recall/qdrant_backend.py` |
| `MEMORYMASTER_QUARANTINE_DIR` | `memorymaster/govern/jobs/fk_repair.py` |
| `MEMORYMASTER_QUERY_CACHE` | `memorymaster/recall/query_cache.py`, `memorymaster/stores/migrations/0004_query_cache.py` |
| `MEMORYMASTER_QUERY_INCLUDE_LEGACY_PROJECT` | `memorymaster/recall/context_hook.py`, `memorymaster/surfaces/mcp_server.py` |
| `MEMORYMASTER_RECALL_` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_ANCESTOR_SCOPES` | `memorymaster/surfaces/mcp_server.py` |
| `MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD_` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_CLOSETS` | `memorymaster/knowledge/closets.py`, `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_CLOSETS_BOOST_ONLY` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_FUSION` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_GRAPH` | `memorymaster/recall/context_hook.py`, `memorymaster/recall/graph_store.py` |
| `MEMORYMASTER_RECALL_GRAPH_CANDIDATES` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_GRAPH_EXPAND_BASE_SHARE` | `memorymaster/recall/graph_expansion.py` |
| `MEMORYMASTER_RECALL_GRAPH_EXPAND_DEADLINE_MS` | `memorymaster/recall/graph_expansion.py` |
| `MEMORYMASTER_RECALL_GRAPH_EXPAND_FANOUT` | `memorymaster/recall/graph_expansion.py` |
| `MEMORYMASTER_RECALL_GRAPH_EXPAND_HOPS` | `memorymaster/recall/graph_expansion.py` |
| `MEMORYMASTER_RECALL_GRAPH_EXPAND_MAX_CANDIDATES` | `memorymaster/recall/graph_expansion.py` |
| `MEMORYMASTER_RECALL_GRAPH_EXPAND_SEEDS` | `memorymaster/recall/graph_expansion.py` |
| `MEMORYMASTER_RECALL_GRAPH_EXPAND_TOKEN_BUDGET` | `memorymaster/recall/graph_expansion.py` |
| `MEMORYMASTER_RECALL_GRAPH_MAX_HOPS` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_GRAPH_MODE` | `memorymaster/core/service.py`, `memorymaster/recall/graph_expansion.py` |
| `MEMORYMASTER_RECALL_GRAPH_PATH` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_QUERY_EXPANSION` | `memorymaster/recall/context_hook.py`, `memorymaster/recall/query_expansion.py` |
| `MEMORYMASTER_RECALL_RERANK_LOCAL` | `memorymaster/recall/context_hook.py`, `memorymaster/recall/local_rerank.py` |
| `MEMORYMASTER_RECALL_RERANK_LOCAL_MAX_PAIRS` | `memorymaster/recall/local_rerank.py` |
| `MEMORYMASTER_RECALL_RERANK_LOCAL_OVERFETCH` | `memorymaster/recall/local_rerank.py` |
| `MEMORYMASTER_RECALL_SCOPE_BOOST` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_STATE_DIR` | `memorymaster/recall/delivery.py` |
| `MEMORYMASTER_RECALL_TWO_PASS` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_TWO_PASS_MAX` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_TWO_PASS_USE_EDGES` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_RECALL_VECTOR_FALLBACK` | `memorymaster/recall/qdrant_recall_fallback.py` |
| `MEMORYMASTER_RECALL_VECTOR_LIMIT` | `memorymaster/recall/qdrant_recall_fallback.py` |
| `MEMORYMASTER_RECALL_VECTOR_MIN_CANDIDATES` | `memorymaster/recall/qdrant_recall_fallback.py` |
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
| `MEMORYMASTER_REVIEW_ATTEMPT_FILE` | `memorymaster/operations/review_attempt.py`, `memorymaster/operations/review_supervisor.py` |
| `MEMORYMASTER_REVIEW_ATTEMPT_ID` | `memorymaster/operations/operational_review.py`, `memorymaster/operations/review_attempt.py`, `memorymaster/operations/review_supervisor.py` |
| `MEMORYMASTER_REVIEW_RESULTS` | `memorymaster/surfaces/dashboard_summary.py` |
| `MEMORYMASTER_ROLES_CONFIG` | `memorymaster/core/access_control.py` |
| `MEMORYMASTER_ROLE_` | `memorymaster/core/access_control.py` |
| `MEMORYMASTER_RRF_TIEBREAKER` | `memorymaster/core/config.py` |
| `MEMORYMASTER_RRF_TIEBREAKER_THRESHOLD` | `memorymaster/core/config.py` |
| `MEMORYMASTER_RULE_CONFIDENCE_BOOTSTRAP` | `memorymaster/knowledge/rule_miner.py` |
| `MEMORYMASTER_SCOPE_DEFAULT` | `memorymaster/recall/context_hook.py` |
| `MEMORYMASTER_SCOPE_DISAMBIGUATE` | `memorymaster/surfaces/mcp_server.py` |
| `MEMORYMASTER_SESSION_DIVERSITY_CAP` | `memorymaster/core/config.py` |
| `MEMORYMASTER_SESSION_END_CAPTURE` | `memorymaster/config_templates/hooks/memorymaster-session-end.py` |
| `MEMORYMASTER_SKILL_REVIEW` | `memorymaster/govern/skill_review_phase.py` |
| `MEMORYMASTER_SKILL_REVIEW_LIMIT` | `memorymaster/govern/skill_review_phase.py` |
| `MEMORYMASTER_SNAPSHOT_DIR` | `memorymaster/govern/jobs/integrity.py`, `memorymaster/profile/engine.py`, `memorymaster/stores/snapshot.py` |
| `MEMORYMASTER_SOURCE_AGENT` | `memorymaster/surfaces/mcp_server.py` |
| `MEMORYMASTER_SPOOL_DIR` | `memorymaster/core/spool.py`, `memorymaster/profile/engine.py` |
| `MEMORYMASTER_STALE_THRESHOLD` | `memorymaster/core/config.py` |
| `MEMORYMASTER_STEWARD_CLASSIFIER_ENABLED` | `memorymaster/govern/steward_classifier.py` |
| `MEMORYMASTER_STEWARD_CLASSIFIER_PATH` | `memorymaster/govern/steward_classifier.py` |
| `MEMORYMASTER_STEWARD_FEATURE_CACHE` | `memorymaster/knowledge/wiki_similarity.py` |
| `MEMORYMASTER_STEWARD_RULE_MINING` | `memorymaster/core/service.py` |
| `MEMORYMASTER_STEWARD_RULE_MINING_LIMIT` | `memorymaster/core/service.py` |
| `MEMORYMASTER_STOP_BLOCKING` | `memorymaster/config_templates/hooks/memorymaster-auto-ingest.py` |
| `MEMORYMASTER_STOP_CAPTURE_VERBATIM` | `memorymaster/config_templates/hooks/memorymaster-auto-ingest.py` |
| `MEMORYMASTER_STOP_EXTRACT` | `memorymaster/config_templates/hooks/memorymaster-auto-ingest.py` |
| `MEMORYMASTER_STOP_RULE_MINING` | `memorymaster/config_templates/hooks/memorymaster-auto-ingest.py` |
| `MEMORYMASTER_STORE_BACKEND` | `memorymaster/core/access_control.py` |
| `MEMORYMASTER_TRANSCRIPTION_PROVIDER` | `memorymaster/capture/worker.py`, `memorymaster/surfaces/setup_hooks.py` |
| `MEMORYMASTER_UNKNOWN_ARGS_DISABLED` | `memorymaster/surfaces/unknown_args.py` |
| `MEMORYMASTER_UNKNOWN_ARGS_LOG` | `memorymaster/surfaces/unknown_args.py` |
| `MEMORYMASTER_USAGE_LEDGER_DB` | `memorymaster/core/usage_ledger.py` |
| `MEMORYMASTER_VALIDATION_THRESHOLD` | `memorymaster/core/config.py` |
| `MEMORYMASTER_VAULT_DIR` | `memorymaster/knowledge/vault_log.py`, `memorymaster/surfaces/mcp_server.py` |
| `MEMORYMASTER_WAL_DISCIPLINE` | `memorymaster/bridges/dream_bridge.py`, `memorymaster/config_templates/hooks/memorymaster-auto-ingest.py`, `memorymaster/config_templates/hooks/memorymaster-dream-sync.py`, `memorymaster/config_templates/hooks/memorymaster-recall.py` (+7 more) |
| `MEMORYMASTER_WEBHOOK_SECRET` | `memorymaster/core/webhook.py` |
| `MEMORYMASTER_WEBHOOK_TIMEOUT` | `memorymaster/core/webhook.py` |
| `MEMORYMASTER_WEBHOOK_URL` | `memorymaster/core/webhook.py` |
| `MEMORYMASTER_WIKI_ABSORB` | `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`, `memorymaster/recall/context_hook.py`, `memorymaster/surfaces/mcp_server.py` |
| `MEMORYMASTER_WIKI_AUTOPROMOTE_THRESHOLD` | `memorymaster/core/lifecycle.py` |
| `MEMORYMASTER_WIKI_DIR` | `memorymaster/knowledge/wiki_engine.py` |
| `MEMORYMASTER_WIKI_ROOT` | `memorymaster/knowledge/wiki_similarity.py` |
| `MEMORYMASTER_WORKFLOW_DB` | `memorymaster/surfaces/cli.py`, `memorymaster/surfaces/setup_hooks.py`, `memorymaster/workflow_intelligence/storage.py` |
| `MEMORYMASTER_WORKFLOW_RECEIPTS` | `memorymaster/surfaces/setup_hooks.py`, `memorymaster/workflow_intelligence/hook.py` |
| `MEMORYMASTER_WORKFLOW_TRANSCRIPT_ROOTS` | `memorymaster/workflow_intelligence/hook.py` |
| `MEMORYMASTER_WORKSPACE` | `memorymaster/public/v1.py`, `memorymaster/surfaces/mcp_http.py`, `memorymaster/surfaces/mcp_server.py`, `memorymaster/surfaces/setup_hooks.py` |
| `MEMORYMASTER_W_CONF` | `memorymaster/core/config.py` |
| `MEMORYMASTER_W_FRESH` | `memorymaster/core/config.py` |
| `MEMORYMASTER_W_LEX` | `memorymaster/core/config.py` |
| `MEMORYMASTER_W_VEC` | `memorymaster/core/config.py` |
