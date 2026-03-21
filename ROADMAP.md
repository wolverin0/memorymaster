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
- [ ] Add `valid_from`, `valid_until`, `event_time` columns to claims
- [ ] Schema migration for existing DBs
- [ ] Time-windowed queries ("what was true on March 15?")
- [ ] CLI: `--as-of` flag for temporal queries

### Automatic Tiering (core / working / peripheral)
- [ ] Track `access_count` and `last_accessed` per claim
- [ ] Auto-promote: access_count >5 OR age <7d → core
- [ ] Auto-demote: access_count 0 AND age >90d → peripheral
- [ ] Tiered retrieval: core claims get priority in context packing
- [ ] MCP: expose tier in query results

### Entity Extraction (LLM-powered)
- [ ] Port `EntityExtractor` from memoryking (Gemini/Ollama)
- [ ] Extract entities (people, projects, servers, APIs) from claim text
- [ ] Entity graph: relationships between extracted entities
- [ ] Entity-based retrieval: "everything about MercadoPago"
- [ ] CLI: `entity-graph` command for visualization

### RL Feedback Loop
- [ ] Port `RLWriteScorer` + `RLTrainer` from memoryking
- [ ] Track which claims are accessed (positive signal) vs ignored
- [ ] Train a lightweight model to predict claim quality
- [ ] Use RL score to influence write policy (skip low-value ingestion)
- [ ] CLI: `feedback stats` command

### Query Classification
- [ ] Port 7 query types from memoryking: fact_lookup, relational, temporal, constraint_check, preference, verification, open_ended
- [ ] Route to optimal retrieval strategy per type
- [ ] Classification via LLM or rule-based heuristics

---

## v2.2.0 — External Integrations

**Theme: Connect to the broader tool ecosystem**

### Obsidian Vault Export
- [ ] `memorymaster export-vault --output <path>` CLI command
- [ ] One `.md` per claim with YAML frontmatter (status, confidence, scope, type)
- [ ] `[[mm-xxxx]]` wikilinks from claim_links
- [ ] Group by scope into subdirectories
- [ ] Incremental export (only changed claims)
- [ ] Compatible with obsidian-skills (agents can read exported vault)

### GitNexus Bridge
- [ ] `scripts/gitnexus_to_claims.py` bridge script
- [ ] Run `npx gitnexus analyze` on projects
- [ ] Ingest architectural claims: clusters, execution flows, key symbols
- [ ] Auto-update on code changes (git hook or scheduled)
- [ ] GitNexus already configured as MCP peer in `.mcp.json`

### OpenClaw Bidirectional Sync
- [ ] Cron-based DB sync (scp every 15 min)
- [ ] Webhook on ingest for real-time push
- [ ] Conflict resolution for simultaneous edits
- [ ] QMD ↔ memorymaster claim mapping

---

## v2.3.0 — Multi-Agent Coordination

**Theme: Multiple agents sharing and curating memory**

- [ ] Claim ownership / agent attribution (who wrote what)
- [ ] Conflict resolution policies (last-writer-wins, confidence-weighted, quorum)
- [ ] Cross-agent memory sharing with scope isolation
- [ ] Agent heartbeat and session tracking
- [ ] Access control: admin / writer / reader roles (from memoryking models)
- [ ] Visibility: public / private claims per agent

---

## v3.0.0 — Autonomous Knowledge Management

**Theme: Self-organizing, self-healing memory**

- [ ] LLM-powered conflict resolution with human approval gates
- [ ] Automatic claim extraction from unstructured text (NER + relation extraction)
- [ ] Knowledge graph visualization (web UI)
- [ ] Cross-project memory federation
- [ ] Plugin system for custom validators and probes
- [ ] Progressive disclosure: summary → detail → full context
- [ ] Docker image with multi-stage build
- [ ] Helm chart for Kubernetes deployment
