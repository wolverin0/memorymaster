<!-- doc-head: Claude Code entrypoint; imports AGENTS.md (shared instructions) and adds only Claude-specific notes -->
<!-- Covers: which .claude/rules/ files auto-load and when, repo skills/workflows, wiki tooling, GitNexus pointer. -->
<!-- Read when: working in Claude Code in this repo; AGENTS.md (imported below) remains the shared source of truth. -->
<!-- Rules: claims-lifecycle, sensitivity-filter, mcp-server always load; python/* on *.py; storage-parity on storage/schema files. -->
<!-- Deeper: docs/development.md#code-intelligence (GitNexus), DOCS-MAP.md (doc index with currency verdicts). -->
<!-- Last verified against the tree: 2026-11-04. -->
<!-- /doc-head -->
# MemoryMaster — Claude-Specific Instructions

@AGENTS.md

## Claude-specific

- GitNexus is available for impact analysis before editing any symbol (see below);
  repo skills live in `.claude/skills/gitnexus/`.
- Repo skills: `.claude/skills/mm-audit`, `mm-feature-rnd`, `mm-mine-corrections`.
  Workflow scripts for the v4 program: `.claude/workflows/mm4-*.js`.
- Wiki: the vault `obsidian-vault/` is local, gitignored and generated (it is not in
  the repo). CLI equivalents of the wiki tooling: `memorymaster wiki-absorb`,
  `memorymaster lint-vault`, `memorymaster curate-vault`. `/wiki ...` and
  `/project-setup` are user-level skills, not shipped in this repo.
- Use `.claude/rules/` for path-specific rules if needed.

## Active Rules (`.claude/rules/`)

Path-scoped rules auto-load when editing matching files. Unscoped rules always load.

**From ECC (`repo/rules/python/` upstream — stack-specific):**
- `python/coding-style.md`, `python/patterns.md`, `python/testing.md`, `python/security.md`, `python/hooks.md` — auto-load on `**/*.{py,pyi}`

**Project-specific:**
- `claims-lifecycle.md` (always-on) — status transitions, promotion policy, tiers, scope conventions, bitemporal fields
- `sensitivity-filter.md` (always-on) — never ingest creds/tokens/personal paths/raw code; private IPs are ingested on purpose and redacted on export; the filter must run on every ingest path
- `storage-parity.md` (scoped to the storage modules, `schema*.sql`, `db_merge.py` and storage/postgres tests) — SQLite + Postgres sync, WAL, FTS5
- `mcp-server.md` (always-on) — MCP tool conventions (auto-citation, sensitivity wrapper, source_agent)

**Curation log:** `.claude/_curation_log_2026-04-18.md` (its backup tarball was local-only and is not in the repo).

## Code intelligence

Follow the shared GitNexus impact and change-detection requirements in
[development guidance](docs/development.md#code-intelligence).
