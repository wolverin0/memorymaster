# MemoryMaster

**Production-grade memory reliability system for AI coding agents.**

Lifecycle-managed claims with citations, conflict detection, steward governance, hybrid retrieval, and MCP integration. Give your AI agents persistent, trustworthy memory.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-3200%2B-green.svg)]()
[![Release truth](https://img.shields.io/badge/release%20truth-generated-purple.svg)](docs/generated/release-truth.md)
[![CLI Commands](https://img.shields.io/badge/CLI%20commands-106-orange.svg)]()
[![PyPI](https://img.shields.io/pypi/v/memorymaster.svg)](https://pypi.org/project/memorymaster/)

MemoryMaster prevents the #1 problem with agent memory: **drift, stale assumptions, and unsafe disclosure**. It gives Claude Code, Codex, and any MCP-compatible agent persistent, verifiable memory with a full claim lifecycle, citation tracking, conflict detection, and human-in-the-loop governance.

> **Product posture:** MemoryMaster is primarily a personal/local application.
> The default profile uses one SQLite database and a private stdio MCP process.
> Postgres/team operation is a deferred optional capability, and Qdrant is an
> optional semantic index—not a dependency or source of truth.

### How it's different

Agent-memory systems (mem0, Letta/MemGPT, Zep, cognee) have largely converged on strong **retrieval** — hybrid search, temporal and graph reasoning, ontologies. MemoryMaster competes on a different axis: **governance**. The wedge is **curation over accumulation** — every memory is a lifecycle-managed *claim* (status, citations, decay, conflict arbitration), not an opaque embedding that lingers until overwritten. Tellingly, the market moved the other way: mem0's 2026 model is explicitly *add-only with no conflict resolution* — the opposite of a steward.

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

MemoryMaster is layered around MCP/CLI entry points, the `MemoryService` facade, authoritative
SQLite storage, an optional deferred Postgres/team backend, an optional Qdrant index, scheduled jobs, and an **optional** Obsidian wiki/vault
layer (opt-in, off by default — see below). The canonical ingest path is:

```text
MCP/CLI -> sensitivity filter -> MemoryService.ingest -> store write -> FTS5 index
```

The query path is:

```text
query_memory -> MemoryService.query -> authorized SQLite rows -> lexical/local-hybrid ranking -> context
```

Qdrant candidate retrieval is disabled by default. When a user deliberately
enables the governed semantic profile, Qdrant may return only candidate IDs,
content hashes, and scores. Every candidate is rehydrated from the authoritative
store and rechecked for lifecycle, tenant, scope, visibility, sensitivity, and
exact content hash. The semantic profile remains unproven for production until
the separately documented authenticated/TLS gate passes.

See [docs/architecture.md](docs/architecture.md) for the current module map, data-flow details,
recent PR status, and sensitivity-filter invariants.

## Key features

- **6-state lifecycle**: `candidate` → `confirmed` → `stale` → `superseded` → `conflicted` → `archived`
- **Citation tracking** with provenance for every claim
- **Hybrid retrieval**: authoritative SQLite claim rows ranked with FTS5, local/primary-store embedding signals, freshness, and confidence; governed Qdrant candidates are explicit and optional
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
- **LLM Wiki** *(opt-in, off by default — set `MEMORYMASTER_WIKI_ABSORB=1`)*: compiled-truth + append-only timeline articles with progressive-disclosure frontmatter, `explored: true|false` operator-review marker, and inline `> [!contradiction]` Obsidian callouts. A redundant human-browsable **view** of the claims DB — the DB + recall is the memory system; the Markdown wiki does not scale past a few hundred pages (Karpathy LLM-Wiki pattern) so it stays off unless you want to browse it
- **Atlas Inbox V1** (new in v3.13.0): WhatsApp ingestion → source/evidence/action proposal lifecycle → Super-Productivity export. Versioned API/CLI contract for downstream consumers (LifeAgent, etc.) — see [`docs/atlas-api-contract-v1.md`](docs/atlas-api-contract-v1.md). Real provider adapters (`OpenAIWhisperTranscriptionProvider`, `TesseractOcrProvider`) behind `Protocol`s; mock providers stay default.
- **Optional local-path resolution** (new in v4.1.0): `resolve-project` / `local-search` (CLI + MCP) turn a fuzzy project/file name into its real on-disk path and cache it as a recallable `reference` claim. Most useful for agents **without** strong native file search (e.g. Codex on Windows) and for cross-session "where was project X?" — for clients that already have a good file-glob this is marginal. Backed by [Everything](https://www.voidtools.com/)'s read-only `ES.exe` CLI via a backend-agnostic `LocalSearchProvider` Protocol (`memorymaster/bridges/local_search/`; `plocate`/`fd`/`mdfind` can drop in later). **Requires Everything + the ES CLI with `MEMORYMASTER_EVERYTHING_ES_PATH` set; degrades to a no-op when absent.** Paths are redacted to root-relative tokens so usernames/structure are never stored.
- **LLM typed-entity Atlas extractor** (new in v4.1.0): turns ingested evidence (WhatsApp / email / notes) into *typed*, cited life-knowledge claims (`person`/`project`/`commitment`/`decision`/`event`/…) via an LLM with strict subject/type validation — replacing the deterministic keyword matcher. The `subject` is always the real named entity, never the source app.
- **Bitemporal write-time guard** (new in v4.2.0): rejects malformed ISO-8601 and inverted `valid_until < valid_from` at ingest, so a durable-but-invisible claim can never be written.
- **`archive_by_source` + `checkpoint`** (new in v4.2.0): lifecycle-safe bulk source cleanup (archive, never hard-delete; dry-run default) and one-round-trip batch ingest through the same sensitivity filter.
- **Intent-aware ranking** (new in v4.2.0, opt-in): `retrieval_profile="auto"` routes query intent to a weight profile; RRF fusion (`MEMORYMASTER_RECALL_FUSION`) is available but `linear` stays default after A/B measurement.
- **Usage telemetry** (new in v4.2.0): per-agent recall counters + a `get_usage_rollup` MCP tool.
- **Guarded fuzzy entity resolver** (new in v4.2.0, opt-in `MEMORYMASTER_ENTITY_FUZZY_RESOLVE`): refuses ambiguous alias matches (anti-hallucination) instead of fragmenting entities.
- **Hebbian/Ebbinghaus entity edges** (new in v4.2.0, opt-in `MEMORYMASTER_HEBBIAN_DECAY`): usage strengthens, time decays entity-graph edge weights.
- **Proactive + tool-triggered recall** (new in v4.2.0): a `volunteer_context` MCP tool (confidence-gated, zero-LLM) and an opt-in PreToolUse hook (`MEMORYMASTER_PRETOOLUSE_RECALL`) that injects memory as `additionalContext` on Grep/Glob.
- **Belief `holder`** (new in v4.2.0): nullable per-claim `holder` for multi-holder beliefs (take/fact/bet/hunch reuse `claim_type`); SQLite+Postgres, ranking-neutral by default.
- **SQLite-first backend**: SQLite for the primary personal/local product; PostgreSQL team support is retained but deferred until a real multi-user use case and its external evidence exist
- **Dream Bridge** for bidirectional sync with Claude Code's Auto Dream
- **Hook stack**: recall, classify, validate-wiki, session-start, auto-ingest, precompact (settings.json) + steward-cycle (cron/schtasks) + opt-in `--pretooluse` grep/glob recall-inject

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
- **An LLM provider** — pick one: Claude Code OAuth (free if you're a subscriber, set `MEMORYMASTER_LLM_PROVIDER=claude_cli`), a free Gemini API key from [aistudio.google.com](https://aistudio.google.com), OpenAI, Anthropic API, or local Ollama. The steward and auto-ingest cycles need an LLM — without one, claims pile up as `candidate` and never get validated or deduped. (The opt-in `wiki-absorb` needs one too, if you enable it.)

**Strongly recommended (you'll lose ~80% of the value without these)**

- **Node.js 18+** for [graphify](https://github.com/wolverin0/graphify) and [GitNexus](https://github.com/wolverin0/gitnexus) — these are the cached intelligence layers that make MemoryMaster cheap to query. Without them, every "what does this codebase do?" question burns tokens cold-exploring files the graph already mapped. The `intelligence-first` workflow in `CLAUDE.md` assumes both are installed.
- **Obsidian 1.6+** with the [Bases](https://help.obsidian.md/Plugins/Bases) core plugin — **only if you opt into the Markdown wiki** (`MEMORYMASTER_WIKI_ABSORB=1`, off by default). The claims DB + recall is the memory system and needs no editor; if you turn `wiki-absorb` on, Obsidian's backlinks/graph/Bases are how you'd browse that (redundant) view. Not needed for normal use.

**Optional (nice to have)**

- **Docker** only if you deliberately want local Ollama or the optional governed Qdrant semantic profile. SQLite remains authoritative and requires neither Docker nor Qdrant.

## 30-second quickstart

**1. Install**

```bash
pip install "memorymaster[mcp,security,qdrant,embeddings]"
```

**2. Let your agent do the rest**

Paste the contents of [`docs/AGENT-INSTALL.md`](docs/AGENT-INSTALL.md) into Claude Code or Codex. The agent will:
- run `memorymaster-setup --yes --profile minimal --no-full-stack --json` (wires the SQLite database and private MCP without starting Postgres, Qdrant, or Ollama)
- report what was wired, what was reused (brownfield), and what degraded
- run `memorymaster-setup --verify-only` and show the round-trip result

**3. Restart your session**

Hooks and MCP load on session start. Restart Claude Code / Codex once.

**4. Verify**

After restart, run in your agent:

```
query_memory("test")
```

You should get a recall response from the MCP server. Done.

---

For manual setup, advanced flags (`--provider`, `--db`, `--no-cron`, `--no-full-stack`, `--verify-only`, `--json`, and more), Docker, Helm, and Postgres, see [INSTALLATION.md](INSTALLATION.md).

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
        "MEMORYMASTER_WORKSPACE": "/path/to/your/project",
        "MEMORYMASTER_MCP_AUTH_MODE": "local-trusted"
      }
    }
  }
}
```

MCP authorization mode is mandatory. Use `local-trusted` only with SQLite in a
private stdio process controlled by one OS user. PostgreSQL application runtime
is team-only and requires an operator-configured principal, tenant, non-owner
application DSN, workspace, and explicit scope allowlist. Schema initialization
uses a distinct migrator DSN/role; never give that role to the MCP runtime.
Unverified host-wide and maintenance tools fail closed. Existing brownfield MCP
entries must add the mode or be regenerated with setup `--force`.

MemoryMaster exposes setup/lifecycle, ingest, query/retrieval, listing, knowledge-graph, and governance tools. The [generated release truth](docs/generated/release-truth.md) is the authoritative inventory and count.

See [`docs/MCP-TOOLS.md`](docs/MCP-TOOLS.md) for the grouped reference (one line per tool), and [`.mcp.json.example`](.mcp.json.example) for the full config template.

## Backends

| Backend | Install | Use case |
|---------|---------|----------|
| **SQLite** | Built-in | **Primary profile:** personal/local, private, zero-config |
| **Postgres 16.x** | `pip install "memorymaster[postgres]"` | **Deferred:** future authenticated team deployment with isolated app/migrator roles |

Postgres is not required for normal MemoryMaster use and is not currently a
release target. The detailed contract below is retained so the dormant profile
fails closed and can be revisited safely if a genuine shared-service use case
appears.

PostgreSQL v0011 enables and forces row-level security. Reads are tenant/scope
bounded and expose public claims or the principal's own private claims; writes
are owner-only, require a nonblank `source_agent` on every team claim, and are
limited to public/private rows. Migration v0012 makes public claim identities
tenant + exact-scope local; non-public idempotency keys, human IDs, and
confirmed tuples additionally include exact visibility and principal. A
tenant-derived hash-only function preserves the event chain across private
principals/scopes without exposing payloads. The application role must read and
append events but cannot update any event column or delete events. Unscoped
human-ID/idempotency-key reads fail when an identifier is ambiguous across
scopes. The supersession guard denies self- and cross-tenant/scope/visibility/
principal links; the canonical lifecycle locks both claims and commits reciprocal
pointers plus one event atomically. Startup rejects drift in exact policy,
function, trigger, privilege, and identity-index catalogs. Brownfield
owner/duplicate/unsafe-supersession repair requires a reviewed external
maintenance action. Team action proposals and raw merge/sync paths remain disabled.
See [INSTALLATION.md](INSTALLATION.md#postgresql-team-runtime-security-boundary)
and [deployment profiles](docs/deployment_profiles.md) before enabling this
backend. Real PostgreSQL verification requires two distinct DSNs targeting a
disposable database; repository tests do not constitute a production proof.

## Docker Compose

For an explicit experimental semantic profile, run the optional Qdrant index
and Ollama stack with:

```bash
docker compose up -d
```

Starting Qdrant alone does not enable governed retrieval. The minimal local
profile should not start this stack.

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
| [docs/MCP-TOOLS.md](docs/MCP-TOOLS.md) | MCP usage guide; generated inventory and counts are linked from the document |
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
