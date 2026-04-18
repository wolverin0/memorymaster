# Curation Log — memorymaster

**Date:** 2026-04-18
**Performed by:** Claude session (per-project-curation pattern, pilot #2)
**Backup:** `.claude/_backups/pre-curate-2026-04-18.tgz` (10.8 KB)

## Context before curation

- Stack: Python 3.10+ (SQLite + FTS5 + WAL, optional Postgres, MCP FastMCP stdio, multi-LLM)
- Existing `.claude/rules/`: 1 file (`mcp-server.md`) — hand-authored, preserved
- Existing `.claude/skills/`: `gitnexus/` only — preserved
- CLAUDE.md + AGENTS.md: already well-curated (previous project-setup pass). GitNexus block embedded in both via `<!-- gitnexus:start -->`
- Uncommitted at start: trivial re-index count bump on CLAUDE.md + AGENTS.md (4194 → 4278 symbols). Benign.
- Noise to archive: **none** — `.claude/` was already clean (contrast with elbraserito which had 23 agent + 29 skill dirs from mass-install)

## Copied from ECC (`everything-claude-code` upstream)

| Source | Destination | Why |
|---|---|---|
| `repo/rules/python/coding-style.md` | `.claude/rules/python/coding-style.md` | PEP 8, immutability, type hints |
| `repo/rules/python/patterns.md` | `.claude/rules/python/patterns.md` | Protocol, context managers, ABCs |
| `repo/rules/python/testing.md` | `.claude/rules/python/testing.md` | pytest conventions |
| `repo/rules/python/security.md` | `.claude/rules/python/security.md` | Secret management, input validation |
| `repo/rules/python/hooks.md` | `.claude/rules/python/hooks.md` | PostToolUse lint/format |

All 5 files have `paths: ["**/*.py", "**/*.pyi"]` frontmatter intact (ECC python rules ship with frontmatter, unlike `web/*.md`).

**Post-copy fix:** stripped the `> This file extends [common/...]` line from all 5 files (we don't copy `rules/common/*` — user has own globals at `~/.claude/rules/` that should win).

## Generated project-specific

| File | Scope | Source of content |
|---|---|---|
| `claims-lifecycle.md` | always-on | AGENTS.md Boundaries + `service.py`/`storage.py` status fields (GitNexus) |
| `sensitivity-filter.md` | always-on | AGENTS.md Boundary ("Never skip the sensitivity filter in dream-seed or MCP ingest") + `mcp_server.py` + `dream_bridge.py` (Key Modules table) |
| `storage-parity.md` | paths-scoped to storage.py, postgres_store.py, schema*.sql, db_merge.py | AGENTS.md Boundary ("Never mutate the claims DB schema without updating storage.py + postgres_store.py + all tests") |

## Preserved untouched

- `.claude/rules/mcp-server.md` — 7 lines, hand-authored
- `.claude/skills/gitnexus/` — GitNexus workflow skills
- `.claude/settings.json`, `.claude/settings.local.json`, `.claude/worktrees/`
- `.claude-flow/`, `.gemini/`, `.serena/`, `.gitnexus/`
- `AGENTS.md` (only CLAUDE.md was modified — appended "Active Rules" section)
- `ARCHITECTURE.md`, `CHANGELOG.md`, `CONTRIBUTING.md`

## What was NOT done (and why)

- **No agent/skill archive** — `.claude/` was already clean; nothing to archive.
- **No ECC skills copied** — user's global skills cover needs; memorymaster-specific skills would duplicate.
- **No ECC agents copied** — user's global agents cover needs.
- **No `rules/common/*` copied** — user has own curated 5-line versions at `~/.claude/rules/`; those win.
- **No `rules/web/*` copied** — server library, no frontend.

## Delta vs elbraserito pilot

| Aspect | elbraserito | memorymaster |
|---|---|---|
| Stack | TS/React/Supabase/MP | Python 3.10+/SQLite |
| ECC rules copied | 8 (5 ts + 3 web) | 5 (python) |
| Project rules generated | 6 | 3 |
| Noise archived | 52 dirs (23 agents + 29 skills) | 0 |
| Pre-existing hand-authored rules | 0 | 1 (preserved) |
| Web/frontmatter fix needed | Yes (web/*.md missing `paths:`) | N/A — no web rules |
| Total rules in final `.claude/rules/` | 14 | 9 |

## Verifier pane

Pending (task #17).
