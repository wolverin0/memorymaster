# Roadmap

## Current: v1.0.0 (Released)

All core tracks complete and tested.

| Track | Status | Description |
|-------|--------|-------------|
| A1 - Core Engine | Done | 6-state lifecycle, event log, citations, idempotency |
| A2 - Retrieval Stack | Done | Hybrid ranking (lexical + vector + freshness + confidence) |
| A11 - Steward Governance | Done | Multi-probe validators, proposal workflow, human override |
| B1 - Operator Runtime | Done | JSONL streaming, checkpointing, progressive retrieval |
| B2 - Connectors | Done | Git, tickets, Slack, email, Jira, GitHub, conversation imports |
| C1 - MCP Server | Done | 12 tools for Claude Code / Codex integration |
| C2 - Dashboard | Done | Real-time HTML UI with SSE, triage actions, conflict view |
| D1 - Performance | Done | SLO benchmarks, p95 gates, throughput floors |
| D2 - Incident Drills | Done | Automated drill runner with signed evidence artifacts |
| D3 - Metrics | Done | Prometheus + JSON export, operator health alerts |
| E1 - Security | Done | Auto-redaction, policy-gated access, Fernet encryption |
| E2 - Postgres Backend | Done | Full parity with SQLite, optional pgvector support |

## v1.1.0 (Planned)

**Theme: Semantic Search & Embedding Upgrades**

- [ ] First-class sentence-transformers integration (MiniLM-L6-v2 default)
- [ ] FTS5 virtual table for SQLite full-text search
- [ ] pgvector HNSW index support for Postgres
- [ ] Configurable embedding provider via `pyproject.toml` or env var
- [ ] Embedding migration tool (re-embed existing claims)

## v1.2.0 (Planned)

**Theme: Multi-Agent Coordination**

- [ ] Claim ownership / agent attribution
- [ ] Conflict resolution policies (last-writer-wins, confidence-weighted, quorum)
- [ ] Cross-agent memory sharing with scope isolation
- [ ] WebSocket push notifications for real-time claim updates
- [ ] Agent heartbeat and session tracking

## v1.3.0 (Planned)

**Theme: Cloud & Team Deployment**

- [ ] Docker image with multi-stage build
- [ ] Helm chart for Kubernetes deployment
- [ ] Team dashboard with RBAC (read/write/admin roles)
- [ ] Webhook integrations (Slack, Discord, PagerDuty)
- [ ] REST API server mode (in addition to MCP stdio)

## v2.0.0 (Future)

**Theme: Autonomous Knowledge Management**

- [ ] LLM-powered conflict resolution (with human approval gates)
- [ ] Automatic claim extraction from unstructured text (NER + relation extraction)
- [ ] Knowledge graph visualization
- [ ] Cross-project memory federation
- [ ] Plugin system for custom validators and probes
