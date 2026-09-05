<!-- doc-head: Claude-specific entrypoint for shared MemoryMaster instructions -->
<!-- Covers: scoped Claude rules and links to shared verification and GitNexus guidance. -->
<!-- Read when: working in Claude Code; AGENTS.md remains the shared entrypoint. -->
<!-- /doc-head -->
# MemoryMaster — Claude-Specific Instructions

@AGENTS.md

## Claude-specific

- GitNexus is available for impact analysis before editing any symbol (see below).
- Use `/wiki query`, `/wiki absorb`, `/wiki lint` for knowledge management.
- Obsidian vault: `obsidian-vault/wiki/project-memorymaster/`
- Use `.claude/rules/` for path-specific rules if needed.
- Run `/project-setup` to regenerate these files if architecture changes significantly.

## Active Rules (`.claude/rules/`)

Path-scoped rules auto-load when editing matching files. Unscoped rules always load.

**From ECC (`repo/rules/python/` upstream — stack-specific, high quality):**
- `python/coding-style.md`, `python/patterns.md`, `python/testing.md`, `python/security.md`, `python/hooks.md` — auto-load on `**/*.{py,pyi}`

**Project-specific (generated from AGENTS.md Boundaries + Key Modules):**
- `claims-lifecycle.md` (always-on) — status transitions, tiers, scope conventions, bitemporal fields
- `sensitivity-filter.md` (always-on) — never ingest creds/IPs/tokens/raw code; filter must run on every ingest path
- `storage-parity.md` (scoped to storage.py, postgres_store.py, schema*.sql, db_merge.py) — SQLite + Postgres sync, WAL, FTS5

**Pre-existing (hand-authored, preserved):**
- `mcp-server.md` — MCP tool conventions (auto-citation, sensitivity wrapper, source_agent)

**Curation log:** `.claude/_curation_log_2026-04-18.md`  
**Backup:** `.claude/_backups/pre-curate-2026-04-18.tgz`


## Code intelligence

Follow the shared GitNexus impact and change-detection requirements in
[development guidance](docs/development.md#code-intelligence).
