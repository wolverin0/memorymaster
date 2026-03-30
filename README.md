# MemoryMaster

**Production-grade memory reliability system for AI coding agents.**

Lifecycle-managed claims with citations, conflict detection, steward governance, hybrid retrieval, and MCP integration. Give your AI agents persistent, trustworthy memory.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-932-green.svg)]()
[![MCP Tools](https://img.shields.io/badge/MCP%20tools-21-purple.svg)]()
[![CLI Commands](https://img.shields.io/badge/CLI%20commands-54%2B-orange.svg)]()

---

MemoryMaster gives AI coding agents **persistent, verifiable memory** with a full claim lifecycle, citation tracking, conflict detection, and human-in-the-loop governance. It prevents the #1 problem with agent memory: **drift, stale assumptions, and unsafe disclosure**.

## Stats

| Metric | Count |
|--------|-------|
| Source modules | 35+ (20,000+ lines) |
| Tests | 932 across 66 test modules |
| MCP tools | 21 |
| CLI commands | 54+ |
| Import connectors | 10+ (Git, Slack, Jira, email, GitHub, conversations) |
| Utility scripts | 30+ (connectors, benchmarks, drills) |

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Agent Runtime                            │
│  (Claude Code / Codex / any MCP-compatible agent)               │
└────────────┬────────────────────────────────┬───────────────────┘
             │ MCP (21 tools)                 │ CLI (50+ commands)
             v                                v
┌─────────────────────────────────────────────────────────────────┐
│                      MemoryMaster Core                          │
│                                                                 │
│  ┌──────────┐  ┌───────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ Ingestor │  │ Extractor │  │ Validator │  │ State Engine  │  │
│  │ (events) │->│ (claims)  │->│ (probes)  │->│ (6-state FSM) │  │
│  └──────────┘  └───────────┘  └──────────┘  └───────────────┘  │
│                                                                 │
│  ┌──────────┐  ┌───────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ Retrieval│  │ Compactor │  │ Steward  │  │  Dashboard    │  │
│  │ (hybrid) │  │ (archive) │  │ (govern) │  │  (HTML+SSE)   │  │
│  └──────────┘  └───────────┘  └──────────┘  └───────────────┘  │
│                                                                 │
│  ┌──────────┐  ┌───────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ Entity   │  │  Skill    │  │ Daily    │  │  Vault        │  │
│  │ Graph    │  │ Evolver   │  │ Notes    │  │  Exporter     │  │
│  └──────────┘  └───────────┘  └──────────┘  └───────────────┘  │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ Dream Bridge — bidirectional sync with Claude Code       │   │
│  │ Auto Dream: seed claims out, ingest corrections back     │   │
│  └──────────────────────────────────────────────────────────┘   │
└────────┬──────────────┬──────────────┬──────────┬───────────────┘
         │              │              │          │
         v              v              v          v
┌──────────────┐ ┌───────────┐ ┌───────────┐ ┌──────────────────┐
│ SQLite /     │ │  Qdrant   │ │  Ollama   │ │ Claude Code      │
│ Postgres     │ │ (vectors) │ │  (LLM)    │ │ Auto Dream       │
│              │ │           │ │           │ │ + Obsidian Vault  │
└──────────────┘ └───────────┘ └───────────┘ └──────────────────┘
```

## Key Features

| Feature | Description |
|---------|-------------|
| **6-State Lifecycle** | `candidate` -> `confirmed` -> `stale` -> `superseded` -> `conflicted` -> `archived` |
| **Citation Tracking** | Every claim links to source evidence with provenance |
| **Hybrid Retrieval** | Vector search (sentence-transformers/Gemini) + FTS5 + freshness + confidence ranking |
| **Context Optimizer** | `query_for_context(budget=4000)` -- auto-curated memory that fits your token budget |
| **Claims Engine** | Structured extraction from unstructured text with deduplication and conflict detection |
| **Entity Graph** | LLM-powered entity extraction with typed relationships between claims |
| **Skill Evolution** | Track and evolve agent skills based on accumulated knowledge patterns |
| **Steward Governance** | Multi-probe validators (filesystem, format, citation, semantic, tool) with proposal review |
| **LLM Steward** | Automated claim validation using configurable LLM providers with round-robin key rotation |
| **Conflict Resolution** | 5-tier auto-resolution: confidence > freshness > citations > LLM > manual |
| **Auto-Redaction** | JWT, GitHub tokens, Bearer, AWS keys, SSH keys + custom patterns scrubbed at ingest |
| **Daily Notes** | Automatic session summarization with ghost note detection (second brain pattern) |
| **Obsidian Export** | Export claims as linked Markdown files for use with My-Brain-Is-Full-Crew |
| **Git Versioning** | Snapshot/rollback/diff via SQLite backup API |
| **Multi-tenancy** | Row-level tenant isolation at the service layer |
| **Dual Backend** | SQLite (zero-config) and Postgres (full feature parity with pgvector) |
| **10+ Connectors** | Git, Slack, Jira, email, GitHub, and conversation imports |
| **Real-time Dashboard** | HTML UI with SSE streaming, conflict view, and triage actions |
| **Federated Query** | Cross-project querying across multiple memory databases |
| **Dream Bridge** | Bidirectional sync with Claude Code's Auto Dream — seed quality-filtered claims into `.claude/memory/`, ingest corrections back, with sensitivity filtering and dedup |
| **GitNexus Bridge** | Convert code intelligence (symbols, call graphs, execution flows) into memory claims for code-aware agent memory |

## Quick Start

```bash
# Install
pip install memorymaster

# Initialize database
memorymaster --db memorymaster.db init-db

# Full setup: hooks, MCP, steward cron, Obsidian skills
python scripts/setup-hooks.py
```

The setup script configures everything interactively:
- **Recall hook** — injects relevant claims into every Claude Code prompt
- **Auto-ingest hook** — uses a cheap LLM (Gemini Flash Lite/GPT-4o-mini/Haiku/Ollama) to extract learnings from each session
- **MCP server** — 21 tools available in all Claude Code & Codex sessions
- **Steward cron** — validates and curates claims every 6 hours
- **CLAUDE.md / AGENTS.md** — appends instructions so Claude and Codex actually use MemoryMaster
- **Obsidian skills** — read/write/search your vault from Claude Code

### Manual Quick Start

```bash
# Initialize database
memorymaster --db memorymaster.db init-db

# Ingest a claim with citation
memorymaster --db memory.db ingest \
  --text "Server uses PostgreSQL 16" \
  --source "session://chat|turn-3|user confirmed"

# Run validation cycle
memorymaster --db memory.db run-cycle

# Query memory (hybrid retrieval)
memorymaster --db memory.db query "database version" --retrieval-mode hybrid

# Context optimizer -- THE killer feature for agents
memorymaster --db memory.db context "auth patterns" --budget 4000 --format xml
```

### Docker Compose

Run the full stack (MemoryMaster + Qdrant + Ollama) with one command:

```bash
docker compose up -d
```

See [INSTALLATION.md](INSTALLATION.md) for detailed setup options including Kubernetes/Helm.

### MCP Server (Claude Code / Codex)

```bash
pip install "memorymaster[mcp]"
```

Add to your `.mcp.json` (see [`.mcp.json.example`](.mcp.json.example)):

```json
{
  "mcpServers": {
    "memorymaster": {
      "command": "memorymaster-mcp",
      "env": {
        "MEMORYMASTER_DEFAULT_DB": "/path/to/memorymaster.db",
        "MEMORYMASTER_WORKSPACE": "/path/to/your/project"
      }
    }
  }
}
```

**21 MCP tools:** `init_db`, `ingest_claim`, `run_cycle`, `run_steward`, `classify_query`, `query_memory`, `query_for_context`, `list_claims`, `redact_claim_payload`, `pin_claim`, `compact_memory`, `list_events`, `open_dashboard`, `list_steward_proposals`, `resolve_steward_proposal`, `extract_entities`, `entity_stats`, `find_related_claims`, `quality_scores`, `recompute_tiers`, `federated_query`

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

# Jira / GitHub / Email / Conversations
python scripts/jira_live_to_turns.py --input config.json --output turns.jsonl
python scripts/github_live_to_turns.py --input config.json --output turns.jsonl
python scripts/email_live_to_turns.py --input config.json --output turns.jsonl
python scripts/conversation_importer.py --input chat.json --output turns.jsonl
```

## Obsidian Integration (My-Brain-Is-Full-Crew)

Export claims as linked Obsidian-compatible Markdown files:

```bash
memorymaster --db memory.db export-vault --output ./obsidian-vault/
memorymaster --db memory.db export-vault --output ./vault/ --scope project:myapp --confirmed-only
```

Use with [My-Brain-Is-Full-Crew](https://github.com/wolverin0/My-Brain-Is-Full-Crew) for Obsidian vault management, daily notes, and ghost note detection.

```bash
# Daily activity summary
memorymaster --db memory.db daily-note

# Find knowledge gaps (topics queried but underexplored)
memorymaster --db memory.db ghost-notes
```

## Dream Bridge (Claude Code Auto Dream Sync)

MemoryMaster integrates with Claude Code's **Auto Dream** memory consolidation system. While Auto Dream provides basic session-to-memory consolidation, MemoryMaster adds structured claims, quality scoring, entity graphs, and security filtering on top.

**The problem Auto Dream solves:** Claude Code accumulates memories across sessions, but they drift, contradict each other, and degrade over time. Auto Dream consolidates them every 24 hours.

**What MemoryMaster adds:** A quality layer — structured claims with confidence scores, decay, deduplication, conflict resolution, and a sensitivity filter that blocks credentials, private IPs, personal paths, and code snippets from leaking into memory files.

```bash
# Export top claims as Claude Code memory files
memorymaster --db memory.db dream-seed --project /path/to/project --max 30

# Import Auto Dream memories back as claims
memorymaster --db memory.db dream-ingest --project /path/to/project

# Bidirectional sync (ingest + re-export)
memorymaster --db memory.db dream-sync --project /path/to/project

# Remove all MemoryMaster-seeded files
memorymaster --db memory.db dream-clean --project /path/to/project

# Automatic: run as part of the steward cycle
memorymaster --db memory.db run-cycle --with-dream-sync --dream-project /path/to/project
```

### How it works

```
MemoryMaster DB ──seed──▶ .claude/projects/<slug>/memory/ ◀── Auto Dream
       ▲                              │                        (24h cycle)
       └────────── ingest ────────────┘
```

1. **dream-seed** queries claims filtered by tier (core/working), quality score, and sensitivity
2. Maps MemoryMaster categories to Auto Dream types (`feedback`, `project`, `user`, `reference`)
3. Writes markdown files with YAML frontmatter that Auto Dream can consolidate
4. **dream-ingest** reads non-MemoryMaster memory files back as claims (captures user corrections)
5. **dream-sync** (or `run-cycle --with-dream-sync`) does both in one pass

### Safety

- Blocks private IPs (`192.168.x.x`, `10.x.x.x`), personal paths, SSH commands
- Blocks credentials (API keys, tokens, passwords, `[REDACTED]` markers)
- Blocks raw code snippets (>50% shell commands)
- Near-duplicate detection (70% word overlap threshold)
- Respects Auto Dream's `.dream.lock` file — never writes during consolidation
- MEMORY.md index capped at 200 lines

## Auto-Ingest Stop Hook (LLM-powered memory capture)

MemoryMaster includes a Claude Code **Stop hook** that automatically extracts learnings from each session using a lightweight LLM. When Claude finishes responding, the hook reads the session transcript, sends the last assistant messages to a cheap/fast LLM, and ingests any non-obvious learnings as candidate claims.

### Supported LLM Providers

| Provider | Env Var | Default Model | Cost |
|----------|---------|---------------|------|
| **Google Gemini** (default) | `GEMINI_API_KEY` | `gemini-3.1-flash-lite-preview` | ~free |
| **OpenAI** | `OPENAI_API_KEY` | `gpt-4o-mini` | ~$0.001/call |
| **Anthropic** | `ANTHROPIC_API_KEY` | `claude-haiku-4-5-20251001` | ~$0.001/call |
| **Ollama** (local) | `OLLAMA_URL` | `llama3.2:3b` | free |

### Configuration

Set your provider and API key as environment variables:

```bash
# Google Gemini (default, cheapest cloud option)
export MEMORYMASTER_LLM_PROVIDER=google
export GEMINI_API_KEY=your-key-here

# OpenAI
export MEMORYMASTER_LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-...

# Anthropic Claude
export MEMORYMASTER_LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-...

# Local Ollama (no API key needed)
export MEMORYMASTER_LLM_PROVIDER=ollama
export OLLAMA_URL=http://localhost:11434

# Optional: override model for any provider
export MEMORYMASTER_LLM_MODEL=gemini-2.5-flash
```

### How it works

1. Claude Code Stop hook fires after each response
2. Script reads last assistant messages from session transcript (JSONL)
3. Sends to configured LLM with curator prompt (extracts max 3 learnings per turn)
4. Ingests as `candidate` claims with `confidence=0.6`
5. Steward cycle (every 6h) validates and promotes good candidates to `confirmed`

The hook never blocks — it always approves the stop. Sensitivity filter rejects any claims containing credentials, private IPs, or tokens.

### Using the LLM provider in your own code

```python
from memorymaster.llm_provider import call_llm, parse_json_response

response = call_llm("Extract key facts:", "The bug was caused by missing RLS policies")
claims = parse_json_response(response)
```

## OpenClaw Integration

MemoryMaster integrates with [OpenClaw](https://github.com/wolverin0/openclaw) for multi-agent orchestration. Claims, entities, and memory context flow between MemoryMaster and the OpenClaw task board via the `federated-query` command and the `openclaw2claude` MCP bridge.

```bash
# Quick install via OpenClaw installer
curl -sSL https://raw.githubusercontent.com/wolverin0/memorymaster/main/scripts/openclaw-install.sh | bash
```

## GitNexus Integration (Code Intelligence)

MemoryMaster pairs with [GitNexus](https://github.com/wolverin0/gitnexus) to bridge **code knowledge** and **agent memory**. GitNexus builds a knowledge graph of your codebase (symbols, relationships, execution flows), and MemoryMaster stores the claims that emerge from working with that code.

### How they work together

| Layer | Tool | What it knows |
|-------|------|---------------|
| **Code structure** | GitNexus | Functions, classes, call graphs, execution flows, blast radius |
| **Agent memory** | MemoryMaster | Facts, decisions, corrections, preferences, entity relationships |
| **Bridge** | `gitnexus_to_claims.py` | Converts GitNexus analysis into MemoryMaster claims |

### Workflow

```bash
# 1. Index your codebase with GitNexus
npx gitnexus analyze

# 2. Convert code intelligence into memory claims
python scripts/gitnexus_to_claims.py --project myapp

# 3. Query with both code and memory context
memorymaster --db memorymaster.db query "auth validation" --retrieval-mode hybrid
```

### Impact analysis before editing

GitNexus provides blast-radius analysis that MemoryMaster can reference:

```bash
# Check what breaks if you change a function
npx gitnexus impact --target "validateUser" --direction upstream

# MemoryMaster remembers past decisions about that function
memorymaster --db memorymaster.db query "validateUser" --retrieval-mode hybrid
```

When both are configured as MCP servers, Claude Code gets **code-aware memory** — it can trace execution flows (GitNexus) and recall past decisions about those flows (MemoryMaster).

## New in v2.0

```bash
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
- **RBAC**: Role-based access control with per-agent role overrides via env vars

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

## Configuration

All behavior is tunable via environment variables or a JSON config file. See [`.env.example`](.env.example) for the complete list.

Key config groups:
- **Retrieval weights**: Lexical, confidence, freshness, vector balance
- **Decay rates**: Per-volatility daily decay
- **Thresholds**: Validation, staleness, conflict margin
- **LLM models**: Extractor, resolver, entity extraction model overrides

## Backends

| Backend | Install | Use case |
|---------|---------|----------|
| **SQLite** | Built-in | Local development, single-agent, zero-config |
| **Postgres** | `pip install "memorymaster[postgres]"` | Team deployment, multi-agent, pgvector search |

## Development

```bash
# Install with all dev dependencies
pip install -e ".[dev,mcp,security,embeddings,qdrant]"

# Run tests (932 tests)
pytest tests/ -q

# Lint and format
ruff check memorymaster/ && ruff format memorymaster/

# Performance benchmarks
python benchmarks/perf_smoke.py

# Evaluation suite
python scripts/eval_memorymaster.py --strict

# Incident drill
python scripts/run_incident_drill.py --dry-run
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full development workflow.

## Documentation

| Document | Description |
|----------|-------------|
| [INSTALLATION.md](INSTALLATION.md) | Setup guide: pip, Docker, Helm, MCP config |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev setup, testing, PR workflow |
| [ARCHITECTURE.md](ARCHITECTURE.md) | System design and subsystem details |
| [USER_GUIDE.md](USER_GUIDE.md) | Usage, MCP integration, troubleshooting |
| [CHANGELOG.md](CHANGELOG.md) | Version history and release notes |
| [ROADMAP.md](ROADMAP.md) | Release plan and future tracks |

## License

[MIT](LICENSE) -- Built by [wolverin0](https://github.com/wolverin0)
