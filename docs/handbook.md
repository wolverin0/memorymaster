# MemoryMaster Handbook

The README is a tour. This is the depth.

If a section here goes stale, that's a bug — open an issue or PR. The CHANGELOG is the source of truth for behavior changes; this handbook explains *how to use* the features the CHANGELOG announces.

---

## Table of contents

- [End-to-end flow](#end-to-end-flow)
- [Hooks installed by `memorymaster-setup`](#hooks-installed-by-memorymaster-setup)
- [Operator runtime](#operator-runtime)
- [Dashboard](#dashboard)
- [Steward governance](#steward-governance)
- [Connectors](#connectors)
- [Obsidian integration](#obsidian-integration)
- [Dream Bridge (Claude Code Auto Dream)](#dream-bridge-claude-code-auto-dream)
- [Auto-Ingest Stop hook details](#auto-ingest-stop-hook-details)
- [Wiki engine + Bases](#wiki-engine--bases)
- [Entity registry + typed relationships](#entity-registry--typed-relationships)
- [OpenClaw integration](#openclaw-integration)
- [GitNexus integration](#gitnexus-integration)
- [Companion stack](#companion-stack)
- [Verify install + troubleshooting](#verify-install--troubleshooting)
- [Performance SLOs](#performance-slos)
- [Security model](#security-model)
- [One-prompt agent install](#one-prompt-agent-install)

---

## End-to-end flow

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
│ (write memory)   │  (Claude Code OAuth haiku / Gemini Flash Lite / Ollama),
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

## Hooks installed by `memorymaster-setup`

| Component | What it does | Runs when |
|-----------|-------------|-----------|
| **Recall hook** | Injects relevant claims into every prompt | Every message you send |
| **Classify hook** | Regex signal matcher (Spanish + English) for DECISION/BUG/GOTCHA/CONSTRAINT/ARCHITECTURE/ENVIRONMENT/REFERENCE; injects routing hints. Zero LLM calls, ~5 ms runtime. | Every message you send |
| **Validate-wiki hook** | PostToolUse warning on `obsidian-vault/wiki/**/*.md` if frontmatter (`title`, `description`, `type`, `scope`, `tags`, `date`) is incomplete or the article has no `[[wikilinks]]` and body > 300 chars. Returns warnings via `hookSpecificOutput.additionalContext` so the agent fixes them in-place. | On Edit/Write of wiki files |
| **SessionStart hook** | Queries the DB for recent claims, last cycle summary (ingest/validate/decay/supersession counts), pending candidates, and most-recently-updated wiki articles. Scope auto-derived from cwd. | Session startup / resume |
| **Auto-ingest hook** | LLM extracts learnings from transcript | Every time Claude stops |
| **PreCompact hook** | Forces save to MemoryMaster before Claude Code compacts context (permanent context loss prevention) | Before context compaction |
| **MCP server** (global) | 22 tools for query/ingest/steward | Always available |
| **CLAUDE.md / AGENTS.md append** | Instructions for Claude / Codex to use MemoryMaster | Read at session start |
| **Steward cron** | Validates, decays, compacts claims | Every 6 hours |
| **Obsidian skills** | Read/write/search vault via CLI | On demand |

All hooks fail silently (`exit 0`) on any exception — they never block the user.

## Operator runtime

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

API endpoints: `/health`, `/api/claims`, `/api/events`, `/api/timeline`, `/api/conflicts`, `/api/review-queue`, `/api/triage/action`, `/api/operator/stream`

## Steward governance

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

### v3 statistical classifier + cadence policy

The v3 statistical classifier + cadence policy are off by default so fresh installs behave like legacy steward. To opt in, set `MEMORYMASTER_STEWARD_CLASSIFIER_ENABLED=1` (or point `MEMORYMASTER_STEWARD_CLASSIFIER_PATH` at a trained `.pkl`) and `MEMORYMASTER_POLICY_MODE=cadence`. Full details, training workflow, and back-test harness live in [`docs/enabling-v2-systems.md`](enabling-v2-systems.md).

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

## Obsidian integration

Export claims as linked Obsidian-compatible Markdown files:

```bash
memorymaster --db memory.db export-vault --output ./obsidian-vault/
memorymaster --db memory.db export-vault --output ./vault/ --scope project:myapp --confirmed-only
```

Use with [My-Brain-Is-Full-Crew](https://github.com/wolverin0/My-Brain-Is-Full-Crew) for vault management, daily notes, and ghost note detection.

```bash
# Daily activity summary
memorymaster --db memory.db daily-note

# Find knowledge gaps (topics queried but underexplored)
memorymaster --db memory.db ghost-notes
```

## Dream Bridge (Claude Code Auto Dream)

MemoryMaster integrates with Claude Code's Auto Dream memory consolidation. While Auto Dream provides basic session-to-memory consolidation, MemoryMaster adds structured claims, quality scoring, entity graphs, and security filtering on top.

**The problem Auto Dream solves**: Claude Code accumulates memories across sessions, but they drift, contradict each other, and degrade over time. Auto Dream consolidates them every 24 hours.

**What MemoryMaster adds**: A quality layer — structured claims with confidence scores, decay, deduplication, conflict resolution, and a sensitivity filter that blocks credentials, private IPs, personal paths, and code snippets from leaking into memory files.

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

1. `dream-seed` queries claims filtered by tier (core/working), quality score, and sensitivity
2. Maps MemoryMaster categories to Auto Dream types (`feedback`, `project`, `user`, `reference`)
3. Writes markdown files with YAML frontmatter that Auto Dream can consolidate
4. `dream-ingest` reads non-MemoryMaster memory files back as claims (captures user corrections)
5. `dream-sync` (or `run-cycle --with-dream-sync`) does both in one pass

### Safety rails

- Blocks private IPs (`192.168.x.x`, `10.x.x.x`), personal paths, SSH commands
- Blocks credentials (API keys, tokens, passwords, `[REDACTED]` markers)
- Blocks raw code snippets (>50% shell commands)
- Near-duplicate detection (70% word overlap threshold)
- Respects Auto Dream's `.dream.lock` file — never writes during consolidation
- MEMORY.md index capped at 200 lines

## Auto-Ingest Stop hook details

The Stop hook automatically extracts learnings from each session using a lightweight LLM. When Claude finishes responding, the hook reads the session transcript, sends the last assistant messages to a cheap/fast LLM, and ingests any non-obvious learnings as candidate claims.

### How it works

1. Claude Code Stop hook fires after each response
2. Script reads last assistant messages from session transcript (JSONL)
3. Sends to configured LLM with curator prompt (extracts max 3 learnings per turn)
4. Ingests as `candidate` claims with `confidence=0.6`
5. Steward cycle (every 6h) validates and promotes good candidates to `confirmed`

The hook never blocks — it always approves the stop. Sensitivity filter rejects any claims containing credentials, private IPs, or tokens.

### Provider configuration

```bash
# Claude Code OAuth (recommended for subscribers, no API key)
export MEMORYMASTER_LLM_PROVIDER=claude_cli

# Google Gemini (cheapest cloud, free tier)
export MEMORYMASTER_LLM_PROVIDER=google
export GEMINI_API_KEY=your-key-here

# OpenAI
export MEMORYMASTER_LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-...

# Anthropic API
export MEMORYMASTER_LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-...

# Local Ollama (no API key needed)
export MEMORYMASTER_LLM_PROVIDER=ollama
export OLLAMA_URL=http://localhost:11434

# Optional: override model for any provider
export MEMORYMASTER_LLM_MODEL=gemini-2.5-flash

# Optional: fallback chain (if primary returns empty / 429 / quota error)
export MEMORYMASTER_LLM_FALLBACK_PROVIDER=ollama
export MEMORYMASTER_LLM_FALLBACK_MODEL=gemma4:e4b
```

### Calling the provider from your own code

```python
from memorymaster.llm_provider import call_llm, parse_json_response

response = call_llm("Extract key facts:", "The bug was caused by missing RLS policies")
claims = parse_json_response(response)
```

## Wiki engine + Bases

`wiki_engine._write_article` emits a rich frontmatter for every absorbed article:

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

These fields enable progressive disclosure: an agent can scan 50 articles' frontmatter before deciding which to read in full, and Obsidian Bases can filter on `type` / `tags` / `date`.

### Obsidian Bases

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

# Wiki absorb also regenerates Bases automatically
python -m memorymaster --db memorymaster.db wiki-absorb --output obsidian-vault
```

## Entity registry + typed relationships

`memorymaster/entity_registry.py` introduces canonical entities with alias resolution. When you ingest a claim about `qdrant`, `Qdrant`, or `QDRANT vector DB`, they all resolve to the same entity via `normalize_alias()`.

| Table | Purpose |
|-------|---------|
| `entities` | Canonical names with `entity_type`, `description`, `aliases_count` |
| `entity_aliases` | Many-to-one alias → entity mapping, normalized |

`service.ingest()` auto-assigns `entity_id` on every claim. Use `mcp__memorymaster__entity_stats` to see the current graph, or `find_related_claims` to walk relationships through the entity.

### Typed relationship link types

`CLAIM_LINK_TYPES` (14 total): `relates_to`, `supersedes`, `derived_from`, `contradicts`, `supports`, `implements`, `configures`, `depends_on`, `deployed_on`, `owned_by`, `tested_by`, `documents`, `blocks`, `enables`.

`traverse_relationships()` BFS method on the storage read layer: filter by `link_types`, `max_depth`, and `direction` (outgoing/incoming/both).

## OpenClaw integration

MemoryMaster integrates with [OpenClaw](https://github.com/wolverin0/openclaw) for multi-agent orchestration. Claims, entities, and memory context flow between MemoryMaster and the OpenClaw task board via the `federated-query` command and the `openclaw2claude` MCP bridge.

```bash
# Quick install via OpenClaw installer
curl -sSL https://raw.githubusercontent.com/wolverin0/memorymaster/main/scripts/openclaw-install.sh | bash
```

## GitNexus integration

MemoryMaster pairs with [GitNexus](https://github.com/wolverin0/gitnexus) to bridge **code knowledge** and **agent memory**. GitNexus builds a knowledge graph of your codebase (symbols, relationships, execution flows), and MemoryMaster stores the claims that emerge from working with that code.

| Layer | Tool | What it knows |
|-------|------|---------------|
| **Code structure** | GitNexus | Functions, classes, call graphs, execution flows, blast radius |
| **Agent memory** | MemoryMaster | Facts, decisions, corrections, preferences, entity relationships |
| **Bridge** | `gitnexus_to_claims.py` | Converts GitNexus analysis into MemoryMaster claims |

```bash
# 1. Index your codebase with GitNexus
npx gitnexus analyze

# 2. Convert code intelligence into memory claims
python scripts/gitnexus_to_claims.py --project myapp

# 3. Query with both code and memory context
memorymaster --db memorymaster.db query "auth validation" --retrieval-mode hybrid
```

When both are configured as MCP servers, Claude Code gets **code-aware memory** — it can trace execution flows (GitNexus) and recall past decisions about those flows (MemoryMaster).

## Companion stack

MemoryMaster is the memory layer; this stack covers the rest. The Intelligence-First Rule (check the cheapest cached layer before exploring raw files) is the reason this combination saves ~70× tokens on typical architectural questions.

| Priority | Tool | What it adds | How to install |
|----------|------|--------------|----------------|
| 1 | **graphify** | Pre-computed architecture map (god nodes, communities, surprising connections) in `graphify-out/GRAPH_REPORT.md` | `npm install -g graphify` → `graphify claude install` → `graphify hook install` → `graphify analyze` |
| 2 | **memorymaster** | 22 MCP tools, 7 hooks, wiki, steward | `memorymaster-setup` |
| 3 | **GitNexus** | Symbol-level impact analysis | `npx gitnexus analyze` |
| 4 | **Serena** | LSP-powered symbol-level read/edit | Global MCP config, see [oraios/serena](https://github.com/oraios/serena) |
| 5 | **context7** | Live library docs | First-party Claude Code MCP, no install |
| opt | **Obsidian CLI** | Vault-aware search from terminal | `npm install -g obsidian-cli` |
| opt | **Qdrant** | External vector search | `docker run -p 6333:6333 qdrant/qdrant` |

## Verify install + troubleshooting

After install, run these three commands:

```bash
# 1. Package + version
python -c "import memorymaster; print('memorymaster', memorymaster.__version__)"
# expect: memorymaster 3.5.0  (or higher)

# 2. DB + CLI
python -m memorymaster --db memorymaster.db query "install smoke test"
# expect: empty result set, no traceback

# 3. MCP server reachable from an agent (after restarting client)
#   mcp__memorymaster__list_claims(limit=5)
# expect: empty list or short list of pre-existing claims, no error
```

If all three pass, hooks are in `~/.claude/hooks/`, MCP server is registered in `~/.claude.json`, and the steward cron is scheduled.

### Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| MCP tools don't appear in agent | Client didn't reload config | Fully quit and reopen Claude Code / Codex — stdio MCP servers only load at startup |
| Auto-ingest hook silent, no claims growing | No LLM provider env var set | Set `MEMORYMASTER_LLM_PROVIDER=claude_cli` (subscribers, no key) or `GEMINI_API_KEY` (free tier) |
| `wiki-absorb` says "no claims to absorb" | Scope mismatch between cwd and claims | `memorymaster --db memorymaster.db query "test"` — check `scope` column on results; then re-run absorb with `--scope project:<name>` |
| Steward cron not running | Windows doesn't have cron | `memorymaster-setup` installs a Task Scheduler entry on Windows, a `launchd` plist on macOS, and a crontab line on Linux — check your platform's scheduler UI |
| `ruff check` fails after install | You're on the dev path and haven't pinned ruff | `pip install -e ".[dev]"` |
| Steward 404s on Gemini after switching providers | Hook used `setdefault` and an inherited shell env still pointed at the old provider | Hook MUST use direct `os.environ["KEY"] = ...` assignment, not `setdefault` |

## Performance SLOs

SLO-driven benchmarks with configurable profiles:

| Metric | Quick Profile | Production Profile |
|--------|--------------|-------------------|
| Ingest p95 | ≤ 60 ms | ≤ 80 ms |
| Ingest throughput | ≥ 80 ops/sec | ≥ 60 ops/sec |
| Query p95 | ≤ 250 ms | ≤ 400 ms |
| Query throughput | ≥ 12 ops/sec | ≥ 8 ops/sec |
| Cycle p95 | ≤ 3.5 s | ≤ 6.0 s |
| End-to-end runtime | ≤ 20 s | ≤ 45 s |

```bash
python benchmarks/perf_smoke.py --slo-config benchmarks/slo_targets.json
```

## Security model

- **Auto-redaction**: JWT, GitHub tokens, Bearer, AWS keys, SSH keys, and custom patterns scrubbed at ingest
- **Policy-gated access**: `--allow-sensitive` requires `MEMORYMASTER_ALLOW_SENSITIVE_BYPASS=1`
- **Non-destructive redaction**: `redact-claim` scrubs claim/citation data with full audit trail
- **Encryption**: Optional Fernet encryption for sensitive payloads (`pip install "memorymaster[security]"`)
- **RBAC**: Role-based access control with per-agent role overrides via env vars

## One-prompt agent install

The fastest way to install MemoryMaster end-to-end is to let an AI agent do it. Open Claude Code, Codex, Cursor, or any agent with shell access in the project directory you want to instrument, and paste the prompt below.

```
Install MemoryMaster end-to-end in this directory. Execute each step and verify it before moving to the next. Stop and ask me if any step needs a secret, credential, or destructive action.

Step 1 — Prerequisites
  • Run `python --version` and confirm 3.10+. If lower, stop and ask me to upgrade.
  • Run `python -m pip --version` to confirm pip is available.

Step 2 — Install the package
  • `pip install "memorymaster[mcp,security]"`
  • Confirm `python -c "import memorymaster; print(memorymaster.__version__)"` reports 3.5.0 or higher.

Step 3 — Initialize the project DB
  • `memorymaster --db memorymaster.db init-db`
  • Confirm the file exists and is non-empty.

Step 4 — Run the interactive setup
  • `memorymaster-setup`
  • Installs 7 Claude Code hooks (recall, classify, validate-wiki, session-start, auto-ingest, precompact, steward-cron), wires the MCP server into ~/.claude.json and ~/.codex/, schedules the steward cron (every 6h), and appends a MemoryMaster section to CLAUDE.md / AGENTS.md.

Step 5 — LLM provider for the auto-ingest hook
  • If the `claude` CLI is on PATH and the user has an active Claude Code session, prefer `MEMORYMASTER_LLM_PROVIDER=claude_cli` (no key required). Confirm with the user before defaulting to it.
  • Otherwise, if any of GEMINI_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY is already set, report which one and continue.
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
