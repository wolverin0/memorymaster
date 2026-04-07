# MemoryMaster

## Mission

Production-grade memory reliability system for AI coding agents. Provides lifecycle-managed claims with citations, conflict detection, steward governance, LLM Wiki (Karpathy/Farza pattern), and MCP integration. Gives AI agents persistent, verifiable, self-evolving memory.

## Stack

- **Language**: Python 3.10+
- **Database**: SQLite (FTS5 + WAL mode) — single-file, no server
- **Vector search**: Qdrant (external, at `192.168.100.186:6333`)
- **LLM providers**: Google Gemini, OpenAI, Anthropic, Ollama (via `llm_provider.py`)
- **Package manager**: pip / setuptools
- **MCP**: FastMCP stdio server (21 tools)
- **Wiki**: Obsidian vault with compiled truth + timeline articles
- **CI**: GitHub Actions (`.github/workflows/ci.yml`)

## Architecture

| Directory | Purpose |
|-----------|---------|
| `memorymaster/` | Core library (54 modules) — service, storage, MCP server, wiki engine, dream bridge, vault tools |
| `tests/` | 67 test modules, 974 tests |
| `scripts/` | 33 utility scripts — importers, sync, setup |
| `config-templates/` | Hook templates for setup-hooks.py installer |
| `obsidian-vault/` | LLM-curated wiki (compiled truth + timeline articles) |
| `obsidian-vault/wiki/` | Active wiki articles by project scope |
| `obsidian-vault/raw/` | Staging area for Obsidian Clipper / manual ingestion |

## Commands

| Command | Purpose |
|---------|---------|
| `python -m pytest tests/` | Run full test suite (974 tests) |
| `python -m memorymaster --db memorymaster.db run-cycle` | Steward validation cycle |
| `python -m memorymaster --db memorymaster.db query "topic"` | Query claims |
| `python -m memorymaster --db memorymaster.db wiki-absorb --output obsidian-vault/wiki` | Absorb claims into wiki |
| `python -m memorymaster --db memorymaster.db lint-vault` | Health check: contradictions, gaps |
| `python -m memorymaster --db memorymaster.db wiki-cleanup --output obsidian-vault/wiki` | Audit and rewrite weak articles |
| `python -m memorymaster --db memorymaster.db wiki-breakdown --output obsidian-vault/wiki` | Find missing articles |
| `python scripts/setup-hooks.py` | Install hooks, MCP, cron, skills |
| `ruff check memorymaster/` | Lint |

## Boundaries

- **Never mutate the claims DB schema** without updating `storage.py` + `postgres_store.py` + all tests
- **Never hardcode IPs, paths, or credentials** — use env vars
- **Never skip the sensitivity filter** in dream-seed or MCP ingest — it blocks credentials
- **The wiki is the READ layer, claims DB is the WRITE layer** — don't write to wiki directly, use `wiki-absorb`
- **WAL mode is mandatory** — prevents DB corruption from concurrent access (MCP + OpenClaw sync)

## MemoryMaster (self-referential)

- Scope: `project:memorymaster`
- This IS the MemoryMaster project — use `query_memory` to check existing architecture decisions before changing code
- Use `ingest_claim` after fixing bugs or making architecture changes (set `source_agent` to your provider name)

## Testing

- Framework: pytest with `pytest.ini` config
- Run: `python -m pytest tests/ -q --tb=short`
- 974 tests across 67 modules
- 1 known flaky: `test_operator.py::test_run_stream_resumes_from_checkpoint_state` (race condition)

## Key Modules

| Module | Responsibility |
|--------|---------------|
| `service.py` | Core service layer — ingest, query, run_cycle |
| `storage.py` | SQLite store — claims, citations, events, FTS5 |
| `mcp_server.py` | FastMCP stdio server (21 tools) + auto-citation + sensitivity filter |
| `wiki_engine.py` | wiki-absorb, wiki-cleanup, wiki-breakdown (Karpathy/Farza) |
| `vault_linter.py` | lint-vault: contradictions, orphans, gaps, stale |
| `vault_log.py` | Append-only log.md chronicle |
| `vault_synthesis.py` | Cross-source synthesis on ingest |
| `vault_query_capture.py` | Save query results as wiki pages |
| `dream_bridge.py` | Dream-seed/ingest/sync with Claude Auto Dream |
| `llm_provider.py` | Multi-provider LLM client (Gemini/OpenAI/Anthropic/Ollama) |
| `context_hook.py` | Recall hook for UserPromptSubmit |
| `db_merge.py` | Bidirectional merge for OpenClaw sync |
