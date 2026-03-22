# Roadmap

## Completed: v2.0.0 (Current)

### Core Engine (v1.0)
- [x] 6-state lifecycle: candidate → confirmed → stale → superseded → conflicted → archived
- [x] Event log, citations, idempotency keys, human IDs
- [x] Hybrid retrieval (lexical + vector + freshness + confidence ranking)
- [x] Steward governance with multi-probe validators, proposal workflow
- [x] Operator runtime with JSONL streaming, checkpointing
- [x] Connectors: Git, tickets, Slack, email, Jira, GitHub, conversation imports
- [x] MCP server (12 tools for Claude Code / Codex)
- [x] Dashboard with SSE, triage actions, conflict view
- [x] Security: auto-redaction, policy-gated access, Fernet encryption
- [x] Postgres backend with full SQLite parity

### Semantic Search (v1.1 → merged into v2.0)
- [x] sentence-transformers integration (MiniLM-L6-v2)
- [x] FTS5 virtual table for SQLite full-text search
- [x] Configurable embedding provider (hash, sentence-transformers, Gemini)
- [x] Lazy-load embeddings (64x faster CLI startup)

### Qdrant Integration (v2.0)
- [x] QdrantBackend with Ollama qwen3-embedding:8b (4096 dims)
- [x] Retry with exponential backoff on embed + upsert
- [x] Batch upsert in sync_all (50x fewer HTTP calls)
- [x] `qdrant-sync` and `qdrant-search` CLI commands
- [x] `retrieval_mode="qdrant"` MCP fast path (~0.5s semantic search)
- [x] MCP query default changed to legacy (3min → 0.1s)

### Performance (v2.0)
- [x] N+1 fix: list_citations_batch + count_citations_batch
- [x] N+1 fix: compact_summaries batch link lookup
- [x] Race condition fix in set_confidence audit trail
- [x] Validator graceful handling of tuple uniqueness conflicts
- [x] URL validation crash fix for malformed IPv6
- [x] 19 lint issues cleaned, ruff config added
- [x] CLI handler extraction (links, snapshots, qdrant)

### Data (v2.0)
- [x] 879 claims migrated from MemoryKing Qdrant collection
- [x] 6,889 claims ingested from 17+ project .planning/ and CLAUDE.md files
- [x] 7,400+ total claims, 3,900+ confirmed
- [x] OpenClaw/Otacon sync configured

---

## v2.1.0 — MemoryKing Intelligence Layer

**Theme: Port MemoryKing's 4 killer features into memorymaster**

Source: `G:\_OneDrive\OneDrive\Desktop\Py Apps\memoryking\src\memoryking\`

### Bi-Temporal Timestamps
- [x] Add `valid_from`, `valid_until`, `event_time` columns to claims
- [x] Schema migration for existing DBs
- [x] Time-windowed queries (`query_as_of` method)
- [x] CLI: `--event-time`, `--valid-from`, `--valid-until` on ingest
- [x] MCP: `ingest_claim` accepts temporal params
- [x] CLI: `--as-of` flag for temporal queries on `query` command

### Automatic Tiering (core / working / peripheral)
- [x] Track `access_count` and `last_accessed` per claim
- [x] Auto-promote: access_count >5 OR age <7d → core
- [x] Auto-demote: access_count 0 AND age >90d → peripheral
- [x] Tiered retrieval: core claims get tier bonus in scoring (+0.15/-0.10)
- [x] CLI: `recompute-tiers` command
- [x] Auto-record access on every query
- [x] MCP: `recompute_tiers` tool
- [x] Expose tier in MCP query results

### Entity Extraction (LLM-powered)
- [x] Port `EntityExtractor` from memoryking (uses Ollama deepseek-coder)
- [x] Extract entities (people, projects, servers, APIs) from claim text
- [x] Entity graph: SQLite tables for entities, edges, claim links
- [x] Graph BFS traversal: find related claims via entity connections
- [x] CLI: `extract-entities`, `entity-stats` commands
- [x] MCP: `extract_entities`, `entity_stats`, `find_related_claims` tools
- [x] Batch entity extraction across all confirmed claims (extract-entities --limit N)
- [x] Entity-based retrieval integrated into query pipeline (enrich_with_entities)

### RL Feedback Loop
- [x] FeedbackTracker: records which claims returned per query
- [x] Quality scoring: retrieval_count + access_count + freshness
- [x] Auto-record feedback on every query (wired into service.query_rows)
- [x] CLI: `feedback-stats`, `quality-scores` commands
- [x] MCP: `quality_scores` tool
- [x] sklearn GradientBoostedTree training when 100+ feedback rows (rl_trainer.py)
- [x] Use quality score to influence write policy (quality_scores integration)
- [x] CLI: `train-model` command

### Query Classification
- [x] 7 query types: fact_lookup, relational, temporal, constraint_check, preference, verification, open_ended
- [x] Rule-based classifier with recommended retrieval mode
- [x] MCP: `classify_query` tool + `auto_classify` param on `query_memory`
- [x] Integrate into CLI `query` command with `--auto-classify` flag

---

## v2.2.0 — External Integrations

**Theme: Connect to the broader tool ecosystem**

### Obsidian Vault Export
- [x] `memorymaster export-vault --output <path>` CLI command
- [x] One `.md` per claim with YAML frontmatter (status, confidence, scope, type)
- [x] `[[mm-xxxx]]` wikilinks from claim_links
- [x] Group by scope into subdirectories
- [x] Index.md with links to all claims
- [ ] Incremental export (only changed claims since last export)
- [x] Compatible with obsidian-skills (agents can read exported vault)

### GitNexus Bridge
- [x] `scripts/gitnexus_to_claims.py` bridge script
- [x] Run `npx gitnexus analyze` on projects (9 projects analyzed)
- [x] Ingest architectural claims: clusters, execution flows, key symbols
- [x] GitNexus installed globally + configured as MCP peer in `.mcp.json`
- [ ] Auto-update on code changes (git hook or scheduled)

### OpenClaw Bidirectional Sync
- [x] Cron-based DB sync (scp every 15 min — configured on OpenClaw)
- [x] memorymaster installed on OpenClaw VM
- [x] export-vault for OpenClaw workspace (.md files in ~/.openclaw/workspace/)
- [x] Webhook on ingest for real-time push (webhook.py)
- [ ] Conflict resolution for simultaneous edits
- [x] QMD ↔ memorymaster claim mapping (qmd_bridge.py)

---

## v2.3.0 — Multi-Agent Coordination

**Theme: Multiple agents sharing and curating memory**

- [x] Claim ownership / agent attribution (source_agent field)
- [x] Visibility: public / private claims per agent
- [x] Conflict resolution policies (LLM-powered auto-resolver with evidence evaluation)
- [x] Cross-agent memory sharing with scope isolation (visibility + RBAC filtering)
- [ ] Agent heartbeat and session tracking
- [x] Access control: admin / writer / reader roles (access_control.py)

---

## v3.0.0 — Autonomous Knowledge Management

**Theme: Self-organizing, self-healing memory**

- [x] LLM-powered conflict resolution with human approval gates (auto_resolver.py + existing steward proposals)
- [x] Automatic claim extraction from unstructured text (auto_extractor.py + extract-claims CLI)
- [x] Knowledge graph visualization (Obsidian graph view via export-vault)
- [ ] Cross-project memory federation
- [x] Plugin system for custom validators and probes (plugins.py + entry points)
- [x] Progressive disclosure: summary → detail → full context (detail_level param)
- [x] Docker image with multi-stage build (Dockerfile + docker-compose.yml)
- [x] Helm chart for Kubernetes deployment (helm/memorymaster/)
