# MemoryMaster

## Mission

Production-grade memory reliability system for AI coding agents. Provides lifecycle-managed claims with citations, conflict detection, steward governance, LLM Wiki (Karpathy/Farza pattern), and MCP integration.

## Stack

- **Language**: Python 3.10+
- **Database**: SQLite (FTS5 + WAL mode) — single-file, no server
- **Vector search**: Qdrant (external, via `QDRANT_URL` env var)
- **LLM providers**: Google Gemini, OpenAI, Anthropic, Ollama (via `llm_provider.py`)
- **Package manager**: pip / setuptools
- **MCP**: FastMCP stdio server
- **Wiki**: Obsidian vault with compiled truth + timeline articles
- **CI**: GitHub Actions (`.github/workflows/ci.yml`)

## Architecture

| Directory | Purpose |
|-----------|---------|
| `memorymaster/` | Core library — service, storage, MCP server, wiki engine, dream bridge, vault tools |
| `tests/` | Test suite (run `python -m pytest tests/ --co -q \| tail -1` for current count) |
| `scripts/` | Utility scripts — importers, sync, setup |
| `config-templates/` | Hook templates for setup-hooks.py installer |
| `obsidian-vault/wiki/` | Active wiki articles by project scope |
| `obsidian-vault/raw/` | Staging area for Obsidian Clipper / manual ingestion |

## Commands

| Command | Purpose |
|---------|---------|
| `python -m pytest tests/ -q --tb=short` | Run test suite |
| `python -m memorymaster --db memorymaster.db run-cycle` | Steward validation cycle |
| `python -m memorymaster --db memorymaster.db query "topic"` | Query claims |
| `python -m memorymaster --db memorymaster.db wiki-absorb --output obsidian-vault/wiki` | Absorb claims into wiki |
| `python -m memorymaster --db memorymaster.db lint-vault` | Health check: contradictions, gaps |
| `python -m memorymaster --db memorymaster.db wiki-cleanup --output obsidian-vault/wiki` | Audit and rewrite weak articles |
| `python scripts/setup-hooks.py` | Install hooks, MCP, cron, skills |
| `ruff check memorymaster/` | Lint |

## Boundaries

- **Never mutate the claims DB schema** without updating `storage.py` + `postgres_store.py` + all tests
- **Never hardcode IPs, paths, or credentials** — use env vars
- **Never skip the sensitivity filter** in dream-seed or MCP ingest
- **The wiki is the READ layer, claims DB is the WRITE layer** — use `wiki-absorb`
- **WAL mode is mandatory** — prevents DB corruption from concurrent access

## MemoryMaster

- Scope: `project:memorymaster`
- Query `query_memory` before architecture decisions
- Ingest with `ingest_claim` after bug fixes or architecture changes (set `source_agent`)

## Testing

- Framework: pytest with `pytest.ini` config
- Run: `python -m pytest tests/ -q --tb=short`
- 1 known flaky: `test_operator.py::test_run_stream_resumes_from_checkpoint_state`

## Verification

After any change, verify:
1. `python -m pytest tests/ -q --tb=short` — tests pass
2. `ruff check memorymaster/` — no lint errors
3. `python -m memorymaster --db memorymaster.db run-cycle` — steward runs without crash
4. If MCP changed: restart Claude Code session and test `query_memory`

## Key Modules

| Module | Responsibility |
|--------|---------------|
| `service.py` | Core service — ingest, query, run_cycle |
| `storage.py` | SQLite store — claims, citations, events, FTS5 |
| `mcp_server.py` | MCP server + auto-citation + sensitivity filter |
| `wiki_engine.py` | wiki-absorb, wiki-cleanup, wiki-breakdown |
| `vault_linter.py` | lint-vault: contradictions, orphans, gaps |
| `dream_bridge.py` | Dream-seed/ingest/sync with Claude Auto Dream |
| `llm_provider.py` | Multi-provider LLM client |
| `context_hook.py` | Recall hook for UserPromptSubmit |
| `db_merge.py` | Bidirectional merge for OpenClaw sync |
