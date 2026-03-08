<p align="center">
  <h1 align="center">MemoryMaster</h1>
  <p align="center">
    <strong>Production-grade memory reliability for AI coding agents</strong>
  </p>
  <p align="center">
    <a href="https://github.com/wolverin0/memorymaster/actions/workflows/ci.yml"><img src="https://github.com/wolverin0/memorymaster/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
    <a href="https://github.com/wolverin0/memorymaster/releases"><img src="https://img.shields.io/github/v/release/wolverin0/memorymaster?color=blue" alt="Release"></a>
    <a href="https://pypi.org/project/memorymaster/"><img src="https://img.shields.io/pypi/v/memorymaster?color=green" alt="PyPI"></a>
    <a href="https://github.com/wolverin0/memorymaster/blob/main/LICENSE"><img src="https://img.shields.io/github/license/wolverin0/memorymaster" alt="License"></a>
    <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+">
    <img src="https://img.shields.io/badge/tests-380%2B%20passed-brightgreen" alt="Tests">
    <img src="https://img.shields.io/badge/coverage-SQLite%20%2B%20Postgres-purple" alt="Backend Coverage">
  </p>
</p>

---

MemoryMaster gives AI coding agents **persistent, verifiable memory** with a full claim lifecycle, citation tracking, conflict detection, and human-in-the-loop governance. It prevents the #1 problem with agent memory: **drift, stale assumptions, and unsafe disclosure**.

## Key Features

| Feature | Description |
|---------|-------------|
| **6-State Lifecycle** | `candidate` -> `confirmed` -> `stale` -> `superseded` -> `conflicted` -> `archived` |
| **Citation Tracking** | Every claim links to source evidence with provenance |
| **Hybrid Retrieval** | Real vector search (sentence-transformers/Gemini) + FTS5 + freshness + confidence |
| **Context Optimizer** | `query_for_context(budget=4000)` — auto-curated memory that fits your token budget |
| **Steward Governance** | Multi-probe validators with auto-validate pipeline after LLM extraction |
| **Claim Graph** | Typed relationships (supersedes, contradicts, supports, derived_from, relates_to) |
| **MCP Integration** | 13 tools for Claude Code, Codex, and any MCP-compatible agent |
| **Real-time Dashboard** | HTML UI with SSE streaming, conflict view, and triage actions |
| **Auto-Redaction** | JWT, GitHub tokens, Bearer, AWS keys, SSH keys + custom patterns |
| **Deduplication** | Embedding similarity + text overlap detection with auto-merge |
| **Conflict Resolution** | 5-tier auto-resolution (confidence > freshness > citations > LLM) |
| **Staleness Detection** | File watcher (mtime + git) auto-flags stale claims |
| **Git Versioning** | Snapshot/rollback/diff via SQLite backup API |
| **Multi-tenancy** | Row-level tenant isolation at the service layer |
| **Dual Backend** | SQLite (zero-config) and Postgres (with full feature parity) |
| **Configurable** | 11 env vars + JSON config for all tunable weights and thresholds |
| **10+ Connectors** | Git, Slack, Jira, email, GitHub, and conversation imports |

## Architecture

```
Agent Runtime
  -> Event Ingestor -> Event Log (append-only)
                      -> Claim Extractor -> Claims Store
                      -> State Engine (6-state lifecycle)
Steward Loop -> Multi-Probe Validators -> Proposals -> Human Review
Operator Runtime -> JSONL Inbox -> Progressive Retrieval -> Maintenance
Query Path -> Hybrid Ranker -> Response Context Builder
Compactor -> Summaries + Citation Graph -> Archive
```

## Quick Start

```bash
pip install memorymaster
```

```bash
# Initialize database
memorymaster --db memory.db init-db

# Ingest a claim with citation
memorymaster --db memory.db ingest \
  --text "Server IP is 10.0.0.2" \
  --source "session://chat|turn-1|user confirmed"

# Run validation cycle
memorymaster --db memory.db run-cycle

# Query memory
memorymaster --db memory.db query "server ip" --retrieval-mode hybrid
```

## MCP Server (Claude Code / Codex)

```bash
pip install "memorymaster[mcp]"
```

Add to your MCP config:

```json
{
  "mcpServers": {
    "memorymaster": {
      "command": "memorymaster-mcp"
    }
  }
}
```

**13 MCP tools available:** `init_db`, `ingest_claim`, `run_cycle`, `query_memory`, `query_for_context`, `list_claims`, `list_events`, `pin_claim`, `compact_memory`, `run_steward`, `list_steward_proposals`, `resolve_steward_proposal`, `open_dashboard`

## Operator Runtime

Process conversation turns from a JSONL inbox with automatic claim extraction, retrieval, and maintenance:

```bash
memorymaster --db memory.db run-operator \
  --inbox-jsonl turns.jsonl \
  --retrieval-mode hybrid \
  --policy-mode cadence \
  --max-idle-seconds 120 \
  --log-jsonl artifacts/operator/events.jsonl
```

Features: restart-safe checkpointing, durable pending-turn queue, progressive tiered retrieval, configurable maintenance cadence, `<private>...</private>` block exclusion.

## Dashboard

```bash
memorymaster --db memory.db run-dashboard --port 8765
```

Open `http://127.0.0.1:8765/dashboard` for:
- Claims table with status filters
- Timeline feed with transition history
- Conflict comparison view
- Review queue with approve/reject actions
- Live SSE operator event stream

**API endpoints:** `/health`, `/api/claims`, `/api/events`, `/api/timeline`, `/api/conflicts`, `/api/review-queue`, `/api/triage/action`, `/api/operator/stream`

## Steward Governance

The steward probes confirmed claims for staleness using multiple validators:

| Probe | What it checks |
|-------|----------------|
| `filesystem_grep` | Does the claim value appear in workspace files? |
| `deterministic_format` | Is the object value well-formed (IP, URL, email, date)? |
| `deterministic_citation_locator` | Do cited sources still exist and match? |
| `semantic_probe` | Does surrounding context still support the claim? |
| `tool_probe` | Does running the relevant tool confirm the value? |

```bash
# Non-destructive audit (proposals only)
memorymaster --db memory.db --workspace . run-steward --mode manual --max-cycles 1

# Apply transitions
memorymaster --db memory.db --workspace . run-steward --mode manual --apply

# Review and resolve proposals
memorymaster --db memory.db steward-proposals --limit 50
memorymaster --db memory.db resolve-proposal --action approve --claim-id 42
```

## Connectors

Import from any source into the operator inbox:

```bash
# Git commits
python scripts/git_to_turns.py --input export.json --output turns.jsonl

# Slack messages
python scripts/slack_live_to_turns.py --input config.json --output turns.jsonl

# Jira issues
python scripts/jira_live_to_turns.py --input config.json --output turns.jsonl

# Email (IMAP)
python scripts/email_live_to_turns.py --input config.json --output turns.jsonl

# Generic AI conversations (OpenAI/Claude/Gemini)
python scripts/conversation_importer.py --input chat.json --output turns.jsonl
```

## New in v2.0

```bash
# Context optimizer — THE killer feature for agents
memorymaster --db memory.db context "auth patterns" --budget 4000 --format xml

# Deduplication
memorymaster --db memory.db dedup --dry-run

# Conflict resolution
memorymaster --db memory.db resolve-conflicts

# Claims needing attention
memorymaster --db memory.db ready

# Claim audit trail
memorymaster --db memory.db history 42

# Claim relationships
memorymaster --db memory.db link 10 20 --type supersedes
memorymaster --db memory.db links 10

# Staleness detection
memorymaster --db memory.db check-staleness --workspace /path/to/project

# LLM compaction summaries
memorymaster --db memory.db compact-summaries --provider gemini --api-key $KEY

# Git-backed versioning
memorymaster --db memory.db snapshot --message "before refactor"
memorymaster --db memory.db rollback snap_abc123

# Stealth mode (local-only experimentation)
memorymaster --stealth init-db

# JSON output for all commands
memorymaster --db memory.db --json list-claims
```

## Security

- **Auto-redaction**: JWT, GitHub tokens, Bearer, AWS keys, SSH keys, and custom patterns scrubbed at ingest
- **Policy-gated access**: `--allow-sensitive` requires `MEMORYMASTER_ALLOW_SENSITIVE_BYPASS=1`
- **Non-destructive redaction**: `redact-claim` scrubs claim/citation data with full audit trail
- **Encryption**: Optional Fernet encryption for sensitive payloads (`pip install "memorymaster[security]"`)

## Performance

SLO-driven benchmarks with configurable profiles:

| Metric | Quick Profile | Production Profile |
|--------|--------------|-------------------|
| Ingest p95 | <= 60ms | <= 80ms |
| Ingest throughput | >= 80 ops/sec | >= 60 ops/sec |
| Query p95 | <= 250ms | <= 400ms |
| Query throughput | >= 12 ops/sec | >= 8 ops/sec |
| Cycle p95 | <= 3.5s | <= 6.0s |
| End-to-end runtime | <= 20s | <= 45s |

```bash
python benchmarks/perf_smoke.py --slo-config benchmarks/slo_targets.json
```

## Backends

| Backend | Install | Use case |
|---------|---------|----------|
| **SQLite** | Built-in | Local development, single-agent, zero-config |
| **Postgres** | `pip install "memorymaster[postgres]"` | Team deployment, multi-agent, pgvector search |

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev,mcp,security]"

# Run tests
pytest tests/ -q

# Run performance benchmarks
python benchmarks/perf_smoke.py

# Run evaluation suite
python scripts/eval_memorymaster.py --strict

# Run incident drill
python scripts/run_incident_drill.py --dry-run
```

## Project Stats

- **31 source modules** (20,000+ lines)
- **380+ tests** across 40+ test modules
- **24 utility scripts** (connectors, benchmarks, drills)
- **13 MCP tools** for agent integration
- **6 API endpoints** + SSE streaming
- **10+ import connectors** (Git, Slack, Jira, email, GitHub, conversations)
- **11 configurable weights** via env vars or JSON config

## Documentation

| Document | Description |
|----------|-------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System design and subsystem details |
| [ROADMAP.md](ROADMAP.md) | Release plan and future tracks |
| [CHANGELOG.md](CHANGELOG.md) | Version history and release notes |
| [USER_GUIDE.md](USER_GUIDE.md) | Setup, usage, MCP integration, troubleshooting |

## License

[MIT](LICENSE) - Built by [wolverin0](https://github.com/wolverin0)
