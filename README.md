# MemoryMaster

**Production-grade memory reliability system for AI coding agents.**

Lifecycle-managed claims with citations, conflict detection, steward governance, hybrid retrieval, and MCP integration. Give your AI agents persistent, trustworthy memory.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-1029-green.svg)]()
[![MCP Tools](https://img.shields.io/badge/MCP%20tools-22-purple.svg)]()
[![CLI Commands](https://img.shields.io/badge/CLI%20commands-64-orange.svg)]()

---

MemoryMaster gives AI coding agents **persistent, verifiable memory** with a full claim lifecycle, citation tracking, conflict detection, and human-in-the-loop governance. It prevents the #1 problem with agent memory: **drift, stale assumptions, and unsafe disclosure**.

## Stats

| Metric | Count |
|--------|-------|
| Source modules | 35+ (20,000+ lines) |
| Tests | 1029 across 68 test modules |
| MCP tools | 22 |
| CLI commands | 64 |
| Import connectors | 10+ (Git, Slack, Jira, email, GitHub, conversations) |
| Utility scripts | 30+ (connectors, benchmarks, drills) |

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Agent Runtime                            │
│  (Claude Code / Codex / any MCP-compatible agent)               │
└────────────┬────────────────────────────────┬───────────────────┘
             │ MCP (22 tools)                 │ CLI (64 commands)
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
| **LLM Wiki** | Karpathy/Farza-style compiled wiki articles (compiled truth + append-only timeline) with `description`/`tags`/`date` frontmatter for progressive disclosure |
| **Obsidian Bases** | Auto-generated `.base` dashboards (all-claims, gotchas, decisions, recent, needs-review) regenerated on every `wiki-absorb` |
| **7-Hook Stack** | Recall + Classify (UserPromptSubmit), Validate-Wiki (PostToolUse), Session-Start (SessionStart), Auto-Ingest (Stop), PreCompact — full memory lifecycle without manual intervention |

## Prerequisites

**Required**
- Python **3.10+** with `pip`
- Claude Code **or** Codex **or** any MCP-compatible agent (for the hooks + MCP integration)

**Optional but recommended**
- A free Gemini API key from [aistudio.google.com](https://aistudio.google.com) — powers the auto-ingest hook at ~zero cost. Fallbacks: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or a local Ollama.
- **Node.js 18+** — only if you want graphify (architecture maps) or GitNexus (code impact analysis)
- **Obsidian 1.6+** with the **Bases** core plugin — only if you want to browse the wiki visually
- **Docker** — only if you want Qdrant for hybrid vector search (SQLite FTS5 is the default and works out of the box)

## Install via Agent (One-Prompt) ⚡

**The fastest way to install MemoryMaster end-to-end is to let an AI agent do it.** Open Claude Code, Codex, Cursor, or any agent with shell access in the project directory you want to instrument, and paste the prompt below. The agent handles pip install, MCP wiring, all 7 hooks, steward cron, LLM provider selection, and verification — you only approve steps and provide an API key when asked.

<details>
<summary><b>📋 Click to copy the one-prompt install</b></summary>

```
Install MemoryMaster end-to-end in this directory. Execute each step and verify it before moving to the next. Stop and ask me if any step needs a secret, credential, or destructive action.

Step 1 — Prerequisites
  • Run `python --version` and confirm 3.10+. If lower, stop and ask me to upgrade.
  • Run `python -m pip --version` to confirm pip is available.

Step 2 — Install the package
  • `pip install "memorymaster[mcp,security]"`
  • Confirm `python -c "import memorymaster; print(memorymaster.__version__)"` reports 3.3.1 or higher.

Step 3 — Initialize the project DB
  • `memorymaster --db memorymaster.db init-db`
  • Confirm the file exists and is non-empty.

Step 4 — Run the interactive setup
  • `memorymaster-setup`
  • This installs 7 Claude Code hooks (recall, classify, validate-wiki, session-start, auto-ingest, precompact, steward-cron), wires the MCP server into ~/.claude.json and ~/.codex/, schedules the steward cron (every 6h), and appends a MemoryMaster section to CLAUDE.md / AGENTS.md in the current project.

Step 5 — LLM provider for the auto-ingest hook
  • If any of GEMINI_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY is already set, report which one and continue.
  • Otherwise, tell me the cheapest option is a free Gemini Flash Lite key from aistudio.google.com and stop until I paste one. Never invent or reuse keys.

Step 6 — Verify the MCP server
  • Tell me to fully restart Claude Code / Codex so the new MCP config loads, then wait for me to confirm.
  • After restart, call mcp__memorymaster__query_memory with text "install smoke test". Expect an empty result set (not an error).
  • Call mcp__memorymaster__list_claims with limit 5. Expect an empty or short list.

Step 7 — Optional: graphify (architecture map, saves ~70x tokens on codebase exploration)
  • `npm install -g graphify`
  • `graphify claude install` — installs the global Claude hook
  • `graphify hook install` — per-project post-commit hook
  • `graphify analyze` — index this project (first run may take a few minutes)

Step 8 — Optional: GitNexus (symbol-level impact analysis before edits)
  • `npx gitnexus analyze` in this project

Step 9 — Report
Print a table with:
  • Component → status (✓ installed / — skipped / ✗ failed)
  • Env vars still required (if any)
  • 3 smoke-test commands I can run myself
  • Absolute paths to: memorymaster.db, modified ~/.claude.json, modified CLAUDE.md

Hard constraints:
  • Do not create new accounts. Do not set credentials for me.
  • Do not edit files outside: this project, ~/.claude/, ~/.codex/, ~/.memorymaster/.
  • If any step fails, report the exact error and stop. Do not retry silently.
  • Do not run `pip install --upgrade pip` or touch system Python.
```

</details>

## Quick Start (Manual)

```bash
# Install
pip install memorymaster

# Initialize database
memorymaster --db memorymaster.db init-db

# Full setup: hooks, MCP, steward cron, Obsidian skills
memorymaster-setup      # after pip install
# or, from a cloned repo:
python scripts/setup-hooks.py
```

The setup command configures everything interactively:
- **Recall hook** — injects relevant claims into every Claude Code prompt
- **Classify hook** — regex signal matcher (DECISION/BUG/GOTCHA/CONSTRAINT/ARCHITECTURE/ENVIRONMENT/REFERENCE) that injects routing hints, Spanish + English
- **Validate-wiki hook** — PostToolUse warning for wiki articles missing frontmatter or wikilinks
- **SessionStart hook** — injects recent claims + cycle summary + pending candidates at session start
- **Auto-ingest hook** — uses a cheap LLM (Gemini Flash Lite/GPT-4o-mini/Haiku/Ollama) to extract learnings from each session, with a block-based checkpoint every 15 human messages
- **PreCompact hook** — forces save to MemoryMaster before Claude Code compacts context (permanent context loss prevention)
- **MCP server** — 22 tools available in all Claude Code & Codex sessions
- **Steward cron** — validates and curates claims every 6 hours
- **CLAUDE.md / AGENTS.md** — appends instructions so Claude and Codex actually use MemoryMaster
- **Obsidian skills** — read/write/search your vault from Claude Code

### Manual Quick Start

```bash
# Initialize database
memorymaster --db memorymaster.db init-db

# Ingest a claim with citation
memorymaster --db memorymaster.db ingest \
  --text "Server uses PostgreSQL 16" \
  --source "session://chat|turn-3|user confirmed"

# Run validation cycle
memorymaster --db memorymaster.db run-cycle

# Query memory (hybrid retrieval)
memorymaster --db memorymaster.db query "database version" --retrieval-mode hybrid

# Context optimizer -- THE killer feature for agents
memorymaster --db memorymaster.db context "auth patterns" --budget 4000 --format xml
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

**22 MCP tools:** `init_db`, `ingest_claim`, `run_cycle`, `run_steward`, `classify_query`, `query_memory`, `query_for_context`, `list_claims`, `redact_claim_payload`, `pin_claim`, `compact_memory`, `list_events`, `search_verbatim`, `open_dashboard`, `list_steward_proposals`, `resolve_steward_proposal`, `extract_entities`, `entity_stats`, `find_related_claims`, `quality_scores`, `recompute_tiers`, `federated_query`

## How It All Works (E2E)

```
YOU SEND A MESSAGE
       │
       ▼
┌──────────────────┐
│ Recall Hook      │  UserPromptSubmit: searches DB for relevant claims,
│ (read memory)    │  injects as invisible context so Claude knows things
│                  │  from previous sessions automatically
└──────────────────┘
       │
       ▼
  CLAUDE / CODEX WORKS
  (edits, debugs, commits)
       │
       ▼
┌──────────────────┐
│ Stop Hook        │  When Claude stops: sends last messages to cheap LLM
│ (write memory)   │  (Gemini Flash Lite / GPT-4o-mini / Haiku / Ollama),
│                  │  extracts max 3 non-obvious learnings, ingests as
│                  │  candidate claims (confidence 0.6)
└──────────────────┘
       │
       ▼
  CLAIM IN DB (status: candidate)
       │
       ├──── every 6h ──── STEWARD CRON validates candidates,
       │                    promotes good ones to confirmed,
       │                    decays old claims, exports to Obsidian vault
       │
       ├──── every 15m ─── OPENCLAW SYNC merges claims bidirectionally
       │                    between your PC and the server (no overwrites)
       │
       └──── every 24h ─── CLAUDE AUTO DREAM consolidates memory files,
                            dream-bridge syncs with MemoryMaster DB
```

### What gets installed by `setup-hooks.py`

| Component | What it does | Runs when |
|-----------|-------------|-----------|
| **Recall hook** | Injects relevant claims into every prompt | Every message you send |
| **Auto-ingest hook** | LLM extracts learnings from transcript | Every time Claude stops |
| **MCP server** (global) | 22 tools for query/ingest/steward | Always available |
| **CLAUDE.md append** | Instructions for Claude to use MemoryMaster | Read at session start |
| **AGENTS.md append** | Instructions for Codex to use MemoryMaster | Read at session start |
| **Steward cron** | Validates, decays, compacts claims | Every 6 hours |
| **Obsidian skills** | Read/write/search vault via CLI | On demand |

### Supported LLM providers for auto-ingest

| Provider | Env var | Default model | Cost |
|----------|---------|---------------|------|
| Google Gemini (default) | `GEMINI_API_KEY` | `gemini-3.1-flash-lite-preview` | ~free |
| OpenAI | `OPENAI_API_KEY` | `gpt-4o-mini` | ~$0.001/call |
| Anthropic | `ANTHROPIC_API_KEY` | `claude-haiku-4-5-20251001` | ~$0.001/call |
| Ollama (local) | `OLLAMA_URL` | `llama3.2:3b` | free |

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
memorymaster --db memorymaster.db run-cycle --with-dream-sync --dream-project /path/to/project
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

## New in v3.2 — Wiki Frontmatter Schema + Hook Automation

This release adds **4 obsidian-mind-inspired patterns** that turn MemoryMaster into a fully passive memory system. You don't need to remember to call MCP tools — hooks do the work:

### New Hooks

| Hook | Event | What it does |
|------|-------|--------------|
| `memorymaster-classify.py` | UserPromptSubmit | Regex signal matcher (Spanish + English, Latin-letter lookarounds for CJK safety). Detects DECISION / BUG_ROOT_CAUSE / GOTCHA / CONSTRAINT / ARCHITECTURE / ENVIRONMENT / REFERENCE in your prompts and injects routing hints so the agent calls `ingest_claim` after the work. Zero LLM calls, ~5 ms runtime. |
| `memorymaster-validate-wiki.py` | PostToolUse (Edit/Write) | Fires only on `obsidian-vault/wiki/**/*.md`. Checks frontmatter (`title`, `description`, `type`, `scope`, `tags`, `date`) and warns if the article is an orphan (no `[[wikilinks]]` and body > 300 chars). Returns warnings via `hookSpecificOutput.additionalContext` so the agent fixes them in-place. |
| `memorymaster-session-start.py` | SessionStart (startup\|resume) | Queries the DB for recent claims, last cycle summary (ingest/validate/decay/supersession counts), pending candidates, and most-recently-updated wiki articles. Injects everything into the session context so the agent starts informed instead of blank. Scope auto-derived from cwd. |

All hooks fail silently (`exit 0`) on any exception — they never block the user.

### Wiki Article Frontmatter Schema

`wiki_engine._write_article` now emits a richer frontmatter for every absorbed article:

```yaml
---
title: Qdrant
description: "Qdrant is deployed on an Ubuntu VM, accessible via localhost:6333..."
type: decision
scope: project:memorymaster
tags: ["decision", "project-memorymaster", "fact"]
claims: [7966, 8062, 8107]
created: 2026-04-09
last_updated: 2026-04-09
date: 2026-04-09
related: ["[[storage]]", "[[vector-search]]"]
---
```

The new fields enable **progressive disclosure**: an agent can scan 50 articles' frontmatter before deciding which to read in full, and Obsidian Bases can filter on `type` / `tags` / `date`.

### Obsidian Bases Generator

`memorymaster/vault_bases.py` writes 5 dynamic dashboards under `obsidian-vault/bases/`:

| Base | Shows |
|------|-------|
| `all-claims.base` | Every wiki article, sortable by date / type / scope |
| `gotchas.base` | Articles with `type=gotcha` or `tags.contains("gotcha")` |
| `decisions.base` | Articles with `type=decision` or `type=architecture` |
| `recent.base` | Articles updated in the last 14 days |
| `needs-review.base` | Articles missing a `description` field |

Bases regenerate automatically on `wiki-absorb` (use `--no-bases` to skip). They are pure YAML — open any `.base` file in Obsidian to see a live filterable view.

### CLI

```bash
# Regenerate Bases manually
python -m memorymaster --db memorymaster.db bases-generate --output obsidian-vault

# Wiki absorb now also regenerates Bases automatically
python -m memorymaster --db memorymaster.db wiki-absorb --output obsidian-vault
```

### Required Tools

- **Python 3.10+** with stdlib `sqlite3` (everything is stdlib — no extra deps)
- **Obsidian 1.6+** with **Bases core plugin enabled** (for `.base` dashboards). The plugin ships with Obsidian — just enable it under Settings → Core plugins.

### Recommended Companion Stack

MemoryMaster is the memory layer, but it's designed to work alongside a small set of tools that each specialise in one layer of agent intelligence. The **Intelligence-First Rule** — check the cheapest cached layer before exploring raw files — is the reason this stack saves 70× tokens on typical architectural questions.

| Priority | Tool | What it adds | How to install |
|----------|------|--------------|----------------|
| 1 | **graphify** | Pre-computed architecture map (god nodes, communities, surprising connections) in `graphify-out/GRAPH_REPORT.md`. The cheapest layer — answers architectural questions without reading a single source file. | `npm install -g graphify` → `graphify claude install` → `graphify hook install` → `graphify analyze` |
| 2 | **memorymaster** ← you are here | 22 MCP tools (`ingest_claim`, `query_memory`, `run_cycle`, `find_related_claims`, etc.), 7 hooks, wiki, steward | `memorymaster-setup` (interactive; or `python scripts/setup-hooks.py` from clone) |
| 3 | **GitNexus** | Symbol-level impact analysis — "what breaks if I change function X" via a pre-built call graph | `npx gitnexus analyze`. See [GitNexus Integration](#gitnexus-integration-code-intelligence). |
| 4 | **Serena** | LSP-powered symbol-level read/edit — read or rewrite one function without opening the whole file | Global MCP config, see [oraios/serena](https://github.com/oraios/serena) |
| 5 | **context7** | Live library docs — never guess an API signature | Already a first-party Claude Code MCP; nothing to install |
| opt | **Obsidian CLI** | Vault-aware search from the terminal | `npm install -g obsidian-cli` (requires Obsidian 1.12+) |
| opt | **Qdrant** | External vector search backend for semantic recall (SQLite FTS5 is the default) | `docker run -p 6333:6333 qdrant/qdrant` |

### Installation

```bash
# Option A — from PyPI (recommended for users)
pip install memorymaster
memorymaster-setup               # interactive installer

# Option B — from a clone (recommended for contributors)
git clone https://github.com/wolverin0/memorymaster.git
cd memorymaster
pip install -e ".[dev,mcp,security]"
python scripts/setup-hooks.py    # 3-line shim calling memorymaster.setup_hooks:main

# Either way, the installer copies hooks from memorymaster/config_templates/hooks/
# to ~/.claude/hooks/ and registers them in ~/.claude/settings.json automatically.
```

### Verify Installation

After install (either path — pip or agent-driven), run these three commands to confirm everything is wired:

```bash
# 1. Package + version
python -c "import memorymaster; print('memorymaster', memorymaster.__version__)"
# expect: memorymaster 3.3.1  (or higher)

# 2. DB + CLI
python -m memorymaster --db memorymaster.db query "install smoke test"
# expect: empty result set, no traceback

# 3. MCP server reachable from an agent
# In Claude Code or Codex (after restarting the client so MCP reloads):
#   mcp__memorymaster__list_claims(limit=5)
# expect: empty list or a short list of pre-existing claims, no error
```

If all three pass, the hooks are in `~/.claude/hooks/`, the MCP server is registered in `~/.claude.json`, and the steward cron is scheduled. The next message you send in that session will trigger the recall hook, and the first time Claude stops, the auto-ingest hook will capture learnings.

### Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| MCP tools don't appear in agent | Client didn't reload config | Fully quit and reopen Claude Code / Codex — stdio MCP servers only load at startup |
| Claims land under `project:<name>:<hash>` scope | Running MCP is pre-v3.3.1 | Restart the agent so the new `_project_scope()` loads. See [New in v3.3](#new-in-v33--entity-registry--typed-relationships--scope-fix). |
| Auto-ingest hook silent, no claims growing | No LLM provider env var set | Set `GEMINI_API_KEY` (free) or `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` |
| `wiki-absorb` says "no claims to absorb" | Scope mismatch between cwd and claims | `memorymaster --db memorymaster.db query "test"` — check `scope` column on results; then re-run absorb with `--scope project:<name>` |
| Steward cron not running | Windows doesn't have cron | `memorymaster-setup` installs a Task Scheduler entry on Windows, a `launchd` plist on macOS, and a crontab line on Linux — check your platform's scheduler UI |
| `ruff check` fails after install | You're on the dev path and haven't pinned ruff | `pip install -e ".[dev]"` — dev extras include pinned lint tools |

### Tests

32 E2E tests in `tests/test_obsidian_mind_patterns.py` validate all 5 components:

```bash
python -m pytest tests/test_obsidian_mind_patterns.py -v
# 32 passed
```

## New in v3.3 — Entity Registry + Typed Relationships + Scope Fix

v3.3 adds three GBrain-inspired patterns and ships a critical data-layer fix surfaced by a 24h audit.

### Entity Registry

`memorymaster/entity_registry.py` introduces canonical entities with alias resolution. When you ingest a claim about `qdrant`, `Qdrant`, or `QDRANT vector DB`, they all resolve to the same entity via `normalize_alias()`. New tables:

| Table | Purpose |
|-------|---------|
| `entities` | Canonical names with `entity_type`, `description`, `aliases_count` |
| `entity_aliases` | Many-to-one alias → entity mapping, normalized |

`service.ingest()` now auto-assigns `entity_id` on every claim. A one-time backfill mapped 684 existing subjects into 312 entities in ~23 ms. Use `mcp__memorymaster__entity_stats` to see the current graph, or `find_related_claims` to walk relationships through the entity.

### RESOLVER.md — MECE Decision Tree

`obsidian-vault/wiki/RESOLVER.md` is an auto-generated decision tree that tells the wiki engine *which* article a new claim belongs to. 10 canonical types (decision, gotcha, constraint, architecture, environment, reference, bug-root-cause, fact, process, glossary) with disambiguation rules and scope routing. It's **M**utually **E**xclusive, **C**ollectively **E**xhaustive — no claim falls through the cracks, no claim lands in two articles.

### Typed Relationships (5 → 14)

`CLAIM_LINK_TYPES` grew from 5 generic (`relates_to`, `supersedes`, `derived_from`, `contradicts`, `supports`) to 14. New domain-specific link types enable graph traversal that actually answers questions:

```
implements    configures    depends_on    deployed_on    owned_by
tested_by     documents     blocks        enables
```

New `traverse_relationships()` BFS method on the storage read layer: filter by `link_types`, `max_depth`, and `direction` (outgoing/incoming/both). Schema migration preserved existing links via `rename → create → copy → drop`.

### Scope Fix + claim_type Normalization (v3.3.1)

A 24-hour audit surfaced three data-quality bugs, all fixed in v3.3.1:

| Bug | Impact | Fix |
|-----|--------|-----|
| `_project_scope()` appended a `sha1(workspace)[:8]` suffix unconditionally | 341 claims fragmented across 6 scopes for the same project | Scope is now canonical `project:<slug>` by default. Set `MEMORYMASTER_SCOPE_DISAMBIGUATE=1` only if you genuinely have two projects with the same directory name. |
| Classify hook emits ALL-CAPS labels (`DECISION`, `GOTCHA`) that flowed straight into `claim_type` | 30 claims with uppercase types didn't match lowercase queries | `service.ingest()` now lowercases `claim_type` before write. |
| Auto-resolver for `conflicted` claims skipped orphan conflicts (no active sibling) | 6 orphan conflicts accumulated indefinitely | Manual cleanup: orphans are marked `superseded` with `replaced_by_claim_id` pointing to the confirmed sibling. |

Migration was applied in-place on existing databases. The v3.3.1 release notes include the full SQL. **After upgrading, restart Claude Code / Codex so the running MCP server picks up the new `_project_scope()` — otherwise same-session ingests still land under the old hashed scope.**

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

# Run tests (1029 tests)
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
