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
- **Wiki article frontmatter is schema-enforced**: every `obsidian-vault/wiki/**/*.md` must have `title`, `description` (50-200 chars), `type`, `scope`, `tags`, `date`, and at least one `[[wikilink]]` if body > 300 chars. The `memorymaster-validate-wiki.py` hook fires warnings on Edit/Write when any is missing. Generated Obsidian Bases (`obsidian-vault/bases/*.base`) regenerate automatically on `wiki-absorb` — do not hand-edit.

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

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **memorymaster** (4137 symbols, 11362 relationships, 269 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## When Debugging

1. `gitnexus_query({query: "<error or symptom>"})` — find execution flows related to the issue
2. `gitnexus_context({name: "<suspect function>"})` — see all callers, callees, and process participation
3. `READ gitnexus://repo/memorymaster/process/{processName}` — trace the full execution flow step by step
4. For regressions: `gitnexus_detect_changes({scope: "compare", base_ref: "main"})` — see what your branch changed

## When Refactoring

- **Renaming**: MUST use `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` first. Review the preview — graph edits are safe, text_search edits need manual review. Then run with `dry_run: false`.
- **Extracting/Splitting**: MUST run `gitnexus_context({name: "target"})` to see all incoming/outgoing refs, then `gitnexus_impact({target: "target", direction: "upstream"})` to find all external callers before moving code.
- After any refactor: run `gitnexus_detect_changes({scope: "all"})` to verify only expected files changed.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Tools Quick Reference

| Tool | When to use | Command |
|------|-------------|---------|
| `query` | Find code by concept | `gitnexus_query({query: "auth validation"})` |
| `context` | 360-degree view of one symbol | `gitnexus_context({name: "validateUser"})` |
| `impact` | Blast radius before editing | `gitnexus_impact({target: "X", direction: "upstream"})` |
| `detect_changes` | Pre-commit scope check | `gitnexus_detect_changes({scope: "staged"})` |
| `rename` | Safe multi-file rename | `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` |
| `cypher` | Custom graph queries | `gitnexus_cypher({query: "MATCH ..."})` |

## Impact Risk Levels

| Depth | Meaning | Action |
|-------|---------|--------|
| d=1 | WILL BREAK — direct callers/importers | MUST update these |
| d=2 | LIKELY AFFECTED — indirect deps | Should test |
| d=3 | MAY NEED TESTING — transitive | Test if critical path |

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/memorymaster/context` | Codebase overview, check index freshness |
| `gitnexus://repo/memorymaster/clusters` | All functional areas |
| `gitnexus://repo/memorymaster/processes` | All execution flows |
| `gitnexus://repo/memorymaster/process/{name}` | Step-by-step execution trace |

## Self-Check Before Finishing

Before completing any code modification task, verify:
1. `gitnexus_impact` was run for all modified symbols
2. No HIGH/CRITICAL risk warnings were ignored
3. `gitnexus_detect_changes()` confirms changes match expected scope
4. All d=1 (WILL BREAK) dependents were updated

## Keeping the Index Fresh

After committing code changes, the GitNexus index becomes stale. Re-run analyze to update it:

```bash
npx gitnexus analyze
```

If the index previously included embeddings, preserve them by adding `--embeddings`:

```bash
npx gitnexus analyze --embeddings
```

To check whether embeddings exist, inspect `.gitnexus/meta.json` — the `stats.embeddings` field shows the count (0 means no embeddings). **Running analyze without `--embeddings` will delete any previously generated embeddings.**

> Claude Code users: A PostToolUse hook handles this automatically after `git commit` and `git merge`.

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
