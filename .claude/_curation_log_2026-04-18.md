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

## Verifier pane (pane 19, plan mode, ~5min work)

**Findings that caught rule drift:**
1. `claims-lifecycle.md` status table listed **invented names** `working`/`active`. Canonical statuses per `models.py:CLAIM_STATUSES` are: `candidate`, `confirmed`, `stale`, `superseded`, `conflicted`, `archived`. **Fixed** — rewrote status table with real names + transition sources + CLI enforcement note.
2. `claims-lifecycle.md` tiers section listed **invented names** `recent`/`compact`/`archive`. Real tiers per `cli_handlers_basic.py:455` are: `core`, `working`, `peripheral`. **Fixed** — rewrote tier section.
3. `storage-parity.md` referenced `storage.py:_ensure_wal()` — function does NOT exist. WAL is set inline at `storage.py:57` inside `SQLiteStore.connect()`'s inner `_open()`. **Fixed** — corrected reference + added the 3 sibling modules (`_storage_read.py`, `_storage_write_claims.py`, `_storage_lifecycle.py`) to scope.

**Verified OK:**
- `db_merge.py` uses `idempotency_key` ✓ (8 occurrences)
- `mcp_server.py` + `dream_bridge.py` both run the sensitivity filter ✓ (9 + 6 occurrences)
- `supersedes_claim_id` / `replaced_by_claim_id` fields exist ✓ (28 files)

**Bonus code observation from verifier** (NOT a rule fix — a suggestion for future):
- `ingest_claim` MCP tool doesn't expose `supersedes_claim_id` as a parameter → agents can't close the bidirectional pair atomically; they depend on steward to notice. Adding an opt-in parameter would close this gap. Noted in `claims-lifecycle.md` so agents don't invent their own workaround.

**Pattern confirmed (2nd pilot):** Generated project-specific rules DO drift from code reality. 2 of 3 rules had invented identifiers on first pass. Verifier-pane pattern caught both. Skill should codify this: every generated rule's file/function/field references must be grep-verified before commit.
