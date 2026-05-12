# MemoryMaster Architecture

This document maps the current `memorymaster/` package as checked out from `origin/main` on branch
`experiment/T28-architecture-refresh`. It is sourced from the files under `memorymaster/*.py` and
`memorymaster/jobs/*.py`, plus `git log -1 -- <file>` for the change column.

## Layered Diagram

```text
Agent runtimes
  Claude Code / Codex / MCP clients / CLI
        |
        v
MCP and CLI layer
  mcp_server.py, cli.py, cli_handlers_*.py, setup_hooks.py
        |
        v
Service layer
  service.py, llm_steward.py, steward.py, lifecycle.py, retrieval.py,
  context_optimizer.py, query_classifier.py, policy.py, security.py
        |
        v
Storage layer
  storage.py + _storage_*.py, postgres_store.py, store_factory.py,
  qdrant_backend.py, verbatim_store.py, graph_store.py
        |
        +----------------------+
        |                      |
        v                      v
Jobs layer                 Wiki and vault layer
  jobs/*.py                  wiki_engine.py, wiki_*.py, vault_*.py,
  scheduler.py               closets.py, daily_notes.py
        |                      |
        +----------+-----------+
                   v
External surfaces
  Obsidian vault, dashboard, Qdrant, LLM providers, Auto Dream, Atlas adapters
```

## Data Flow

**Ingest path**

```text
MCP client or CLI
  -> mcp_server.py:ingest_claim or service.py:ingest
  -> security.redact_text sensitivity filter
  -> MemoryService.ingest
  -> SQLiteStore.add_claim / PostgresStore.add_claim
  -> claims + citations + events tables
  -> claims_fts FTS5 index
  -> optional Qdrant upsert when QDRANT_URL is configured
  -> optional entity graph / wiki/vault side effects through later jobs
```

`mcp_server.py` validates tool inputs, rejects sensitive ingest payloads, and records usage where
`mcp_usage.py` is wired. Direct service callers still pass through `MemoryService.ingest`, so the
service boundary remains the last storage guard even when MCP is bypassed.

**Query path**

```text
query_memory / CLI query
  -> mcp_server.py or cli.py
  -> MemoryService.query / query_rows
  -> SQLiteStore.query reads claims_fts + claim metadata
  -> retrieval.py ranks rows by lexical, confidence, freshness, graph, and vector signals
  -> optional qdrant_backend.py semantic candidates or fallback rerank
  -> context_optimizer.py packs query_for_context results into provider-aware budgets
```

`query_for_context` reuses the ranked rows and then chooses a text, XML, or JSON envelope that fits
the caller's token budget. Qdrant is an optional acceleration and recall path; the SQLite claim store
remains authoritative.

## Sensitivity Invariant

The project rule in `.claude/rules/sensitivity-filter.md` defines the ingest filter as a storage
boundary. The invariant is:

- `mcp_server.py:ingest_claim` filters every MCP ingest call.
- `service.py:ingest` filters direct service writes.
- `dream_bridge.py` filters Auto Dream imports/exports before they become claims.
- Any new ingest path starts default-deny until wired to `security.redact_text`.
- Display-time masking is separate and cannot replace ingest-time filtering.

ADR 0006 keeps this boundary narrow for Atlas Inbox: raw `source_items` and `evidence_items` preserve
explicitly imported source content, while claims extracted from them still go through `service.ingest`.
The T06 fix in [PR #69](https://github.com/wolverin0/memorymaster/pull/69) extends that rule to
`jobs/compact_summaries.py` by redacting claim text before LLM summarization; that PR is open and not
part of the current `origin/main` tree.

## Documentation Divergence

The `AGENTS.md` Key Modules table is accurate for its listed files, but it is intentionally small.
The current package contains many additional modules that are not represented there, including
`mcp_usage.py`, `context_optimizer.py`, `wiki_suggest.py`, `jobs/calibration.py`, and
`jobs/entity_graph_export.py`.

Requested modules `memorymaster/jobs/csv_import.py`, `memorymaster/jobs/backup_restore.py`, and
`memorymaster/jobs/scope_export.py` are not present in this worktree. Backup and restore behavior is
currently implemented by `memorymaster/snapshot.py`; observability is currently dashboard and hook-log
behavior in `dashboard.py`, `hook_log.py`, and `metrics_exporter.py`, not a standalone
`observability.py` module.

Root `ARCHITECTURE.md` still describes the v1-v3.2 design contract. This file is the current code map.

## Recent Additions, PRs #50-#70

| PR | Status in current tree | Summary |
| --- | --- | --- |
| [#50](https://github.com/wolverin0/memorymaster/pull/50) | Present | Adds `jobs/calibration.py` to compute 90-day validation priors from event history. |
| [#51](https://github.com/wolverin0/memorymaster/pull/51) | Present | Adds `wiki_suggest.py` and CLI support for entity-graph wikilink suggestions. |
| [#52](https://github.com/wolverin0/memorymaster/pull/52) | Present | Documents cross-project patterns from `query_meta_decisions`. |
| [#53](https://github.com/wolverin0/memorymaster/pull/53) | Present | Adds validation latency metrics to dashboard/metrics surfaces. |
| [#54](https://github.com/wolverin0/memorymaster/pull/54) | Present | Adds provider-aware token packing in `context_optimizer.py`. |
| [#55](https://github.com/wolverin0/memorymaster/pull/55) | Present | Applies calibration priors through configuration defaults. |
| [#56](https://github.com/wolverin0/memorymaster/pull/56) | Present | Expands OpenClaw bidirectional DB merge coverage. |
| [#57](https://github.com/wolverin0/memorymaster/pull/57) | Present | Adds Dream Bridge spool polling and export roundtrip coverage. |
| [#58](https://github.com/wolverin0/memorymaster/pull/58) | Present | Adds `mcp_usage.py`, the usage table path, and `mcp-usage-report`. |
| [#59](https://github.com/wolverin0/memorymaster/pull/59) | Present | Adds mobile-friendly dashboard review-queue API support. |
| [#60](https://github.com/wolverin0/memorymaster/pull/60) | Present | Adds `jobs/entity_graph_export.py` with DOT and GraphML export. |
| [#61](https://github.com/wolverin0/memorymaster/pull/61) | Open PR | Security audit docs for MCP and dashboard surfaces. |
| [#62](https://github.com/wolverin0/memorymaster/pull/62) | Open PR | Lifecycle edge-case scenario documentation. |
| [#63](https://github.com/wolverin0/memorymaster/pull/63) | Open PR | Windows, Codex sandbox, and git hook troubleshooting docs. |
| [#64](https://github.com/wolverin0/memorymaster/pull/64) | Open PR | Wiki frontmatter compliance audit documentation. |
| [#65](https://github.com/wolverin0/memorymaster/pull/65) | Open PR | T04 fix for compactor artifact write order before archive status changes. |
| [#66](https://github.com/wolverin0/memorymaster/pull/66) | Open PR | ADR for wiki article auto-promotion after repeated validations. |
| [#67](https://github.com/wolverin0/memorymaster/pull/67) | Open PR | Cross-project query contract documentation. |
| [#68](https://github.com/wolverin0/memorymaster/pull/68) | Open PR | T05 fix so dedup treats object mismatches as conflicts instead of duplicates. |
| [#69](https://github.com/wolverin0/memorymaster/pull/69) | Open PR | T06 fix to redact compact-summary claim text before LLM calls. |
| [#70](https://github.com/wolverin0/memorymaster/pull/70) | Open PR | T08 test proving Dream Bridge ingest runs the sensitivity filter. |

## Module Map

The map below includes first-level Python modules under `memorymaster/` and job modules under
`memorymaster/jobs/`. Subpackages such as `memorymaster/connectors/` and template files are outside
this track's requested inventory.

<!-- module-map:start -->
<!-- module-map:end -->
