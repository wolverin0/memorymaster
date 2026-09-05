<!-- doc-head: concise MemoryMaster implementation instructions -->
<!-- Covers: code map, governance boundaries and scoped verification. -->
<!-- Read when: changing MemoryMaster; deeper guidance is in docs/development.md. -->
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
  CI owns the platform matrix. Preserve useful regression coverage.
- Report source-tested, installed and live-verified separately. Restart only
  affected long-lived services when deployment requires it.
- Keep GitNexus impact analysis before symbol edits and change detection before
  commits; details and index preservation are in `docs/development.md`.
- Record non-obvious fixes as scoped memory, and recurring mistakes as runnable
  regression checks. No new process without a demonstrated failure.
