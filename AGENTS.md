<!-- doc-head: shared agent entrypoint for changing MemoryMaster (all agents; CLAUDE.md and GEMINI.md import it) -->
<!-- Covers: start-of-task checks, code map of memorymaster/ subpackages, critical governance boundaries, finish/verify gates. -->
<!-- Read when: before any code, schema, hook or doc change in this repo; read once per task, then follow the links. -->
<!-- Gates: tests/test_public_demo.py, Ruff on changed files, scripts/bulk_check.py (only merge gate; no hosted CI). -->
<!-- Deeper: docs/development.md (verification, GitNexus), DOCS-MAP.md (every doc + whether current), ROADMAP.md. -->
<!-- Last verified against the tree: 2026-11-04 (all paths and commands below exist). -->
<!-- /doc-head -->
# MemoryMaster

Governed, cited memory for coding agents. SQLite owns claim lifecycle and
authorization; generated observations, profiles, wiki and workflow analytics
are derived views.

## Start here

- Run `python scripts/check_branch_freshness.py`; preserve unrelated work.
- Read `DOCS-MAP.md` before deeper docs. `ROADMAP.md` is the sole roadmap.
- Query project memory before architectural decisions. Current source and dated
  runtime evidence take precedence over old status claims.
- See [development guidance](docs/development.md) for verification and GitNexus.

## Code map

| Path | Responsibility |
|---|---|
| `memorymaster/public/v1.py` | remember / recall / forget / improve facade |
| `memorymaster/core/` | service, models, sensitivity, provider clients |
| `memorymaster/stores/` | authoritative SQLite, migrations, deferred Postgres |
| `memorymaster/capture/` | source/evidence lineage and bounded job processing |
| `memorymaster/recall/` | authorized retrieval, ranking and context packing |
| `memorymaster/govern/` | steward and lifecycle |
| `memorymaster/knowledge/` | supported graph, observations, optional wiki |
| `memorymaster/profile/` | compiled profile and exact support manifests |
| `memorymaster/dreaming/` | asynchronous extraction/consolidation |
| `memorymaster/surfaces/` | CLI, MCP, dashboard and setup |
| `memorymaster/config_templates/` | installed hooks and configuration |
| `memorymaster/workflow_intelligence/` | disposable trajectory analytics |

## Critical boundaries

- SQLite with WAL is authoritative; vectors only propose IDs for authorized
  rehydration. Generated output must not reinforce its own evidence.
- Preserve scope/tenant isolation, citations, sensitivity filtering and
  steward-controlled promotion on every intake and recall path.
- Schema changes use versioned migrations and appropriate store/restore tests.
  Explicit SQLite-only features must keep PostgreSQL fail-closed.
- Dreaming application, profile/observation generation and workflow hooks have
  separate activation decisions. Inspect effective configuration; do not enable
  a disabled feature to make a checkpoint pass.
- Do not print secrets, private addresses or personal paths in reports.
- Never use the authoritative database as a routine test fixture or run a
  mutating steward cycle merely to validate a code/documentation edit.

## Finish a change

- Exercise the affected user outcome and relevant regression tests. Start with
  `python -m pytest tests/test_public_demo.py -q` for the disposable lifecycle.
- Run Ruff on changed Python files. Use the full non-ML suite for broad changes;
  `python scripts/bulk_check.py` is the only merge gate (no hosted CI since
  2026-10-25); without `--fast` it refuses to run outside a wezbridge heavy slot
  (`node "<Py Apps>/wezbridge/scripts/heavy-slot.cjs" run --label bulk-check -- env
  MEMORYMASTER_HEAVY_SLOT=1 python scripts/bulk_check.py`; hosts without wezbridge
  set `MEMORYMASTER_BULK_NO_SLOT=1`). Preserve useful regression coverage.
- Report source-tested, installed and live-verified separately. Restart only
  affected long-lived services when deployment requires it.
- Keep GitNexus impact analysis before symbol edits and change detection before
  commits; details and index preservation are in `docs/development.md`.
- Record non-obvious fixes as scoped memory, and recurring mistakes as runnable
  regression checks. No new process without a demonstrated failure.
