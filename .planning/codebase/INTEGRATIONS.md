# INTEGRATIONS.md — External Integrations

*Regenerated 2026-06-09 from the current tree (v3.28.0). Supersedes the stale v2.0.0 document.*

## Model Context Protocol (MCP)
- **File:** `mcp_server.py` — FastMCP stdio server, `mcp>=1.2` extra, entry point `memorymaster-mcp`
- **30 `@mcp.tool` definitions** (query_memory, query_for_context, query_for_task, ingest_claim, ingest_rule, list_claims, list_events, pin_claim, run_cycle, run_steward, redact_claim_payload, recall_analysis, search_verbatim, federated_query, steward proposal tools, entity tools, etc.)
- Conventions enforced by `.claude/rules/mcp-server.md`: auto-citation fallback, sensitivity filter on every write path, `source_agent` always passed
- **Deployment reality:** each Claude Code pane spawns its OWN stdio mcp_server process against the shared `memorymaster.db` — there is no central daemon (see CONCERNS.md #1)
- Path policy in `mcp_path_policy.py`; usage telemetry in `mcp_usage.py`
- Env: `MEMORYMASTER_DEFAULT_DB`, `MEMORYMASTER_WORKSPACE`, `MEMORYMASTER_DEFAULT_PROJECT_SCOPE`

## Qdrant Vector Database
- **Files:** `qdrant_backend.py` (httpx REST client: `ensure_collection`, `upsert_claim`, `delete_claim`, `search`, `_batch_upsert`, `sync_all`), `qdrant_recall_fallback.py`, `scripts/index_claims_to_qdrant.py`
- **Activation:** `QDRANT_URL` env var; `MemoryService._init_qdrant()` (service.py:308) enables it at construction
- **Sync model:** `_qdrant_sync(claim)` fires on claim upsert/delete (service.py:323, called at service.py:507) and **swallows all exceptions**; `_qdrant_post_cycle_sync()` re-upserts after `run_cycle` (service.py:335, 604). `sync_all` is a manual full rebuild. No continuous reconciliation — see CONCERNS.md #3
- `vector` extra adds `qdrant-client` + sentence-transformers (deterministic 384-dim CPU embeddings)

## LLM Providers (`llm_provider.py`)
- **Multi-provider router**: `MEMORYMASTER_LLM_PROVIDER = google | openai | anthropic | claude_cli | ollama` (default: google)
- **`claude_cli` provider** (keyless): shells out to the local `claude --print` binary using Claude Code OAuth — `_call_claude_cli` (llm_provider.py:291), binary override via `MEMORYMASTER_CLAUDE_CLI_BIN`, degrades with `logger.warning` on missing binary / timeout / nonzero exit. Steward extraction routes through this since commit b62a042
- **Gemini** (`google-genai>=1.0`, `gemini` extra): primary steward LLM — `llm_steward.py`, `jobs/compact_summaries.py`, transcript mining; multi-key rotation + budget in `llm_budget.py`, `key_rotator.py`
- LLM rerank (`llm_rerank.py`) gated by `MEMORYMASTER_LLM_RERANK`

## PostgreSQL
- **Files:** `postgres_store.py` (`psycopg[binary]>=3.2`), `schema_postgres.sql`, selected via `store_factory.py`
- **Parity is contract-enforced**: migrations carry `apply_postgres` alongside `apply_sqlite`; `tests/conftest.py` `parametrize_backends` runs the same test bodies on both backends (v3.20.0-S2); v3.27 batch 1 closed the remaining parity gaps (commit 9a9c3d6)

## Embeddings & Graph
- **sentence-transformers** (`embeddings.py`, `embeddings`/`vector` extras) — semantic similarity for dedup/recall; keyword fallback when absent
- **Kuzu** (`graph_store.py`, `graph` extra) — embedded single-file graph DB for the graph retrieval stream; runtime-gated by `MEMORYMASTER_RECALL_GRAPH=1`, off by default
- **ML extra** (scikit-learn, joblib) — `steward_classifier.py`, `steward_features.py`, `rl_trainer.py`

## Cryptography
- `security.py` + `cryptography>=42` (`security` extra) — sensitive-claim payload encryption at rest; keys from env only

## Sync & Bridges
- **OpenClaw sync:** `db_merge.py` — bidirectional claim merge with the VM (cron every 15 min); opens the shared DB with WAL + `busy_timeout=30000` (db_merge.py:283-292)
- **Dream bridge:** `dream_bridge.py` — dream-seed/ingest/sync with Claude Auto Dream; sensitivity filter mandatory
- **Delta sync:** `delta_sync.py`; **federation:** `federated_graphify.py`, MCP `federated_query`
- **Hooks ecosystem:** `setup_hooks.py` (`memorymaster-setup`) installs SessionStart/recall/classify/stop hooks from `config_templates/hooks/*.py`; `context_hook.py` is the UserPromptSubmit recall hook
- **Connectors:** `connectors/whatsapp.py`; **webhook:** `webhook.py`; **metrics:** `metrics_exporter.py`, `observability.py`
- **Obsidian vault:** `wiki_engine.py`, `vault_*` modules write/lint `obsidian-vault/wiki/` (wiki = read layer, claims DB = write layer)
