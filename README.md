# MemoryMaster

**Production-grade memory reliability system for AI coding agents.**

Lifecycle-managed claims with citations, conflict detection, steward governance, hybrid retrieval, and MCP integration. Give your AI agents persistent, trustworthy memory.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-2927-green.svg)]()
[![MCP Tools](https://img.shields.io/badge/MCP%20tools-30-purple.svg)]()
[![CLI Commands](https://img.shields.io/badge/CLI%20commands-104-orange.svg)]()
[![PyPI](https://img.shields.io/pypi/v/memorymaster.svg)](https://pypi.org/project/memorymaster/)

MemoryMaster prevents the #1 problem with agent memory: **drift, stale assumptions, and unsafe disclosure**. It gives Claude Code, Codex, and any MCP-compatible agent persistent, verifiable memory with a full claim lifecycle, citation tracking, conflict detection, and human-in-the-loop governance.

### How it's different

Most agent-memory systems (mem0, Letta/MemGPT, Zep) optimize for **storing and recalling more** — embeddings, summaries, a fast vector store. MemoryMaster optimizes for **trusting what you recall**. The differentiator is **governance**: every memory is a lifecycle-managed *claim*, not an opaque embedding.

| | mem0 / Letta / Zep | **MemoryMaster** |
|---|---|---|
| Unit of memory | text chunk / summary | **claim** with status, tier, citations, bitemporal validity |
| Stale / wrong facts | linger until overwritten | **decay → `stale`**, **conflict detection**, **supersession** |
| Contradictions | silently coexist | surfaced as **`conflicted`**, auto-resolved (5-tier) or queued for review |
| Provenance | usually none | **citation per claim** + per-agent provenance |
| Secret leakage | your problem | **sensitivity filter at ingest** (JWT/AWS/Bearer/SSH redaction) |
| Operator control | API only | **steward governance** + a **dashboard** you can *see* |

If you want an agent that recalls more, any vector store works. If you want an agent that recalls *correctly* — and can prove where a fact came from and retire it when it goes stale — that's the gap MemoryMaster fills.

---

## Architecture

MemoryMaster is layered around MCP/CLI entry points, the `MemoryService` facade, SQLite/Postgres
storage, optional Qdrant vector search, scheduled jobs, and the Obsidian wiki/vault layer. The
canonical ingest path is:

```text
MCP/CLI -> sensitivity filter -> MemoryService.ingest -> store write -> FTS5 index
```

The query path is:

```text
query_memory -> MemoryService.query -> storage reads + optional Qdrant candidates -> ranked context
```

See [docs/architecture.md](docs/architecture.md) for the current module map, data-flow details,
recent PR status, and sensitivity-filter invariants.

## Key features

- **6-state lifecycle**: `candidate` → `confirmed` → `stale` → `superseded` → `conflicted` → `archived`
- **Citation tracking** with provenance for every claim
- **Hybrid retrieval**: vector (sentence-transformers / Gemini) + FTS5 + freshness + confidence
- **Context optimizer**: `query_for_context(budget=4000)` returns auto-curated memory that fits your token budget
- **Entity graph** with typed relationships and alias resolution
- **Rule-shaped claims** (new in v3.21.0): prescriptive `when <trigger>, do <action> because <rationale>` claims (`ingest_rule` / `query_rules`) — the shape an agent needs to actually change behaviour next time, not just recall a fact
- **Correction mining** (new in v3.21.0): `mine-rules` scans the verbatim transcript archive for user corrections and distills them into rule claims; the Stop hook also mines each session's latest correction automatically
- **Versioned schema migrations** (new in v3.20.0): `migrate` applies SQLite/Postgres migrations with sha256 drift detection; incremental `export-delta` ships small claim deltas for cheap cross-machine sync
- **Retrieval quality** (new in v3.22.0): floor-ratio boost gate (`MEMORYMASTER_BOOST_FLOOR_RATIO`) stops fresh-but-wrong claims outranking the true match; `query --explain` shows per-stage score attribution; an opt-in correctness-safe query cache (`MEMORYMASTER_QUERY_CACHE`) with a generation gate
- **Semantic contradiction probe** (new in v3.22.0, wired as a steward phase in v3.23.0): `detect-contradictions` finds claims that genuinely contradict each other (beyond the deterministic same-subject conflict check) via an LLM judge with a Wilson-CI rate and verdict cache; in v3.23 the same probe runs inside `run-steward` and emits paste-ready `conflicted` proposals
- **Verbatim archive cleanup** (new in v3.23.0): `verbatim-cleanup` dedups the raw-transcript table and optionally purges pre-#128 junk rows, with a dry-run default and FTS5 mirror sync
- **Steward governance**: multi-probe validators (filesystem, format, citation, semantic, tool) with proposal review
- **Conflict resolution**: 5-tier auto (confidence > freshness > citations > LLM > manual)
- **Auto-redaction** at ingest: JWT, GitHub tokens, Bearer, AWS keys, SSH keys, custom patterns
- **LLM Wiki**: compiled-truth + append-only timeline articles with progressive-disclosure frontmatter, `explored: true|false` operator-review marker, and inline `> [!contradiction]` Obsidian callouts
- **Atlas Inbox V1** (new in v3.13.0): WhatsApp ingestion → source/evidence/action proposal lifecycle → Super-Productivity export. Versioned API/CLI contract for downstream consumers (LifeAgent, etc.) — see [`docs/atlas-api-contract-v1.md`](docs/atlas-api-contract-v1.md). Real provider adapters (`OpenAIWhisperTranscriptionProvider`, `TesseractOcrProvider`) behind `Protocol`s; mock providers stay default.
- **Dual backend**: SQLite (zero-config) and Postgres (full feature parity with pgvector)
- **Dream Bridge** for bidirectional sync with Claude Code's Auto Dream
- **7-hook stack**: recall, classify, validate-wiki, session-start, auto-ingest, precompact, steward-cron

Full feature index lives in [`docs/handbook.md`](docs/handbook.md).

## Benchmarks

**LongMemEval-S (N=500, retrieval-only)** — v3.15.0 now leads the publicly-reported numbers from [agentmemory](https://github.com/rohitg00/agentmemory) on R@5 and MRR, after wiring `sentence-transformers/all-MiniLM-L6-v2` into the bench harness (the v3.14 baseline was unintentionally BM25-only).

![LongMemEval-S benchmark](docs/benchmark-longmemeval.svg)

| Metric | v3.14.0 | **v3.15.0** | agentmemory | Δ vs agentmemory |
|---|---|---|---|---|
| Recall@5 | 0.894 | **0.966** | 0.952 | **+0.014** ★ |
| Recall@10 | 0.942 | **0.984** | 0.986 | -0.002 |
| MRR | 0.799 | **0.902** | 0.882 | **+0.020** ★ |

Reproduce: `python tests/bench_longmemeval.py --retrieval-only`. Full methodology, experiment-by-experiment deltas (1 KEEP, 2 REVERT, 3 NULL), and the architectural findings that surfaced along the way live in [`docs/archive/longmemeval-results.md`](docs/archive/longmemeval-results.md) and [`docs/archive/v315-experiments/`](docs/archive/v315-experiments/). QA-accuracy pass (with judge) is deferred until provider quotas allow.

## Prerequisites

**Required (the package won't function without these)**

- Python **3.10+** with `pip`
- Claude Code, Codex, or any MCP-compatible agent
- **An LLM provider** — pick one: Claude Code OAuth (free if you're a subscriber, set `MEMORYMASTER_LLM_PROVIDER=claude_cli`), a free Gemini API key from [aistudio.google.com](https://aistudio.google.com), OpenAI, Anthropic API, or local Ollama. The steward, auto-ingest, and wiki-absorb cycles all need an LLM — without one, claims pile up as `candidate` and never get validated, deduped, or compiled into the wiki.

**Strongly recommended (you'll lose ~80% of the value without these)**

- **Node.js 18+** for [graphify](https://github.com/wolverin0/graphify) and [GitNexus](https://github.com/wolverin0/gitnexus) — these are the cached intelligence layers that make MemoryMaster cheap to query. Without them, every "what does this codebase do?" question burns tokens cold-exploring files the graph already mapped. The `intelligence-first` workflow in `CLAUDE.md` assumes both are installed.
- **Obsidian 1.6+** with the [Bases](https://help.obsidian.md/Plugins/Bases) core plugin — the wiki engine writes plain Markdown so any editor works, but Obsidian's backlinks, graph view, and Bases dashboards are how you actually navigate `wiki-absorb` output. Without Obsidian, the wiki is just a folder of files.

**Optional (nice to have)**

- **Docker** for Qdrant — vector retrieval. SQLite FTS5 is the default and works out of the box; add Qdrant when you want semantic recall on top of keyword search.

## 15-minute quickstart

From zero to a recalled claim and a live dashboard. No Qdrant, no Postgres, no LLM key required for these steps (SQLite + FTS5 is the default).

**1. Install (2 min)**

```bash
pip install "memorymaster[mcp]"
memorymaster --db memorymaster.db init-db
```

**2. Configure a provider (3 min, optional for this walkthrough)**

Recall and ingest below work with zero config. An LLM provider is only needed for the steward/wiki cycles — pick one when you're ready (see [Pick your LLM provider](#pick-your-llm-provider)). For a Claude Code subscriber, the cheapest path is:

```bash
export MEMORYMASTER_LLM_PROVIDER=claude_cli   # reuses your Claude Code OAuth, no API key
```

**3. Ingest a claim via CLI (1 min)**

```bash
memorymaster --db memorymaster.db ingest \
  --text "Server uses PostgreSQL 16" \
  --source "session://chat|turn-3|user confirmed"
```

**4. Recall it (1 min)**

A freshly-ingested claim starts life as a `candidate` (unvalidated). The CLI `query`/`context` paths *exclude* candidates by default — that's the governance model: unvalidated facts don't silently leak into recall until the steward promotes them. To see your brand-new claim before a validation cycle, pass `--include-candidates`:

```bash
# Hybrid retrieval (lexical + freshness + confidence)
memorymaster --db memorymaster.db query "database version" \
  --retrieval-mode hybrid --include-candidates

# Token-budgeted context block — the killer feature for agents
memorymaster --db memorymaster.db context "database" \
  --budget 4000 --format xml --include-candidates
```

You should see the PostgreSQL 16 claim come back, ranked, with its citation. (Drop `--include-candidates` and you'll get zero results until step 7's `run-cycle` promotes it to `confirmed` — that's working as designed, not a bug.)

**5. Open the dashboard (2 min)**

```bash
memorymaster --db memorymaster.db run-dashboard   # serves on http://127.0.0.1:8765
```

Open the URL: you'll see your claim in **Claims**, plus governance panels — **Conflicts**, **Review Queue**, **Recall Analysis** (why each claim ranked where it did), **Audit Log**, **Provenance by Agent**, and **Reliability**.

**6. Wire it into your agent (3 min)**

```bash
memorymaster-setup     # interactive: hooks, MCP, steward cron, CLAUDE.md / AGENTS.md
```

That installs the MCP server and the auto-ingest Stop hook so your agent recalls and stores memory automatically. See [MCP server](#mcp-server) for the config block.

**7. Run a validation cycle (1 min, needs a provider)**

```bash
memorymaster --db memorymaster.db run-cycle   # extract, validate, decay, compact
```

For the one-prompt agent install (paste into any agent with shell access), see [`docs/handbook.md#one-prompt-agent-install`](docs/handbook.md#one-prompt-agent-install).

## Pick your LLM provider

| Provider | Env vars | Default model | Cost |
|----------|----------|---------------|------|
| **Claude Code OAuth** (recommended for subscribers) | `MEMORYMASTER_LLM_PROVIDER=claude_cli` (requires `claude` CLI on PATH) | `claude-haiku-4-5-20251001` | included in Claude Code plan |
| Google Gemini (default) | `MEMORYMASTER_LLM_PROVIDER=google` + `GEMINI_API_KEY=...` | `gemini-3.1-flash-lite-preview` | ~free |
| OpenAI | `MEMORYMASTER_LLM_PROVIDER=openai` + `OPENAI_API_KEY=...` | `gpt-4o-mini` | ~$0.001/call |
| Anthropic API | `MEMORYMASTER_LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY=...` | `claude-haiku-4-5-20251001` | ~$0.001/call |
| Ollama (local) | `MEMORYMASTER_LLM_PROVIDER=ollama` + `OLLAMA_URL=http://localhost:11434` | `llama3.2:3b` | free |

The `claude_cli` provider shells out to your local `claude --print` binary, so it inherits the OAuth session you're already logged into in Claude Code — no API key, no rotator, no quota juggling. **Caveat**: cold-start adds 3-15s per call (subprocess spawn), so it's ideal for batched/cron paths (steward, wiki-absorb) and not for latency-sensitive recall. Override with `MEMORYMASTER_CLAUDE_CLI_BIN` and `MEMORYMASTER_CLAUDE_CLI_TIMEOUT`. On VM installs the OAuth token expires ~24h, so pair with `MEMORYMASTER_LLM_FALLBACK_PROVIDER=ollama`; desktop tokens don't expire.

For zero-cost offline use, install [Ollama](https://ollama.com), `ollama pull llama3.2:3b`, and set `MEMORYMASTER_LLM_PROVIDER=ollama`.

## MCP server

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

30 MCP tools spanning setup/lifecycle, ingest, query/retrieval, listing, knowledge graph, and governance: `init_db`, `ingest_claim`, `ingest_rule`, `query_rules`, `rules_export`, `run_cycle`, `run_steward`, `classify_query`, `query_memory`, `query_for_context`, `query_for_task`, `query_claim_paths`, `query_meta_decisions`, `federated_query`, `recall_analysis`, `read_active_tasks`, `list_claims`, `redact_claim_payload`, `pin_claim`, `compact_memory`, `list_events`, `search_verbatim`, `open_dashboard`, `list_steward_proposals`, `resolve_steward_proposal`, `extract_entities`, `entity_stats`, `find_related_claims`, `quality_scores`, `recompute_tiers`.

See [`docs/MCP-TOOLS.md`](docs/MCP-TOOLS.md) for the grouped reference (one line per tool), and [`.mcp.json.example`](.mcp.json.example) for the full config template.

## Backends

| Backend | Install | Use case |
|---------|---------|----------|
| **SQLite** | Built-in | Local development, single-agent, zero-config |
| **Postgres** | `pip install "memorymaster[postgres]"` | Team deployment, multi-agent, pgvector search |

## Docker Compose

Run the full stack (MemoryMaster + Qdrant + Ollama) with one command:

```bash
docker compose up -d
```

See [INSTALLATION.md](INSTALLATION.md) for Kubernetes / Helm.

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev,mcp,security,embeddings,qdrant]"

# Run tests
pytest tests/ -q

# Lint and format
ruff check memorymaster/ && ruff format memorymaster/

# Performance benchmarks
python benchmarks/perf_smoke.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow.

## Documentation

| Document | Description |
|----------|-------------|
| [docs/README.md](docs/README.md) | Documentation index — where to find each living doc |
| [docs/handbook.md](docs/handbook.md) | Full operator handbook — hooks, dashboard, steward, dream bridge, troubleshooting, one-prompt install |
| [docs/MCP-TOOLS.md](docs/MCP-TOOLS.md) | Reference for all 30 MCP tools, grouped by purpose |
| [docs/INTEGRATING.md](docs/INTEGRATING.md) | Integration guide for embedding MemoryMaster in your agent |
| [INSTALLATION.md](INSTALLATION.md) | Setup guide: pip, Docker, Helm, MCP config |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev setup, testing, PR workflow |
| [ARCHITECTURE.md](ARCHITECTURE.md) | System design and subsystem details |
| [USER_GUIDE.md](USER_GUIDE.md) | Usage, MCP integration, troubleshooting |
| [CHANGELOG.md](CHANGELOG.md) | Version history and release notes |
| [ROADMAP.md](ROADMAP.md) | Release plan and future tracks |
| [docs/enabling-v2-systems.md](docs/enabling-v2-systems.md) | v3 statistical classifier + cadence policy opt-in |

## License

[MIT](LICENSE) — Built by [wolverin0](https://github.com/wolverin0)
