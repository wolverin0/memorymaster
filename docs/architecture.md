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

## Core and companion extension boundary

The authoritative memory system lives in `memorymaster.core`, `memorymaster.stores`,
`memorymaster.recall`, and `memorymaster.govern`: claims, lifecycle, citations,
policy, retrieval, conflict/stewardship, and telemetry remain usable without
loading optional integrations.

Optional product integrations are built-in companion namespaces composed only
at explicit surfaces:

| Companion | Owned namespace | Composition roots |
|---|---|---|
| Wiki and Obsidian | `memorymaster.knowledge.wiki_*`, `memorymaster.knowledge.vault_*` | CLI/MCP handlers, wiki jobs and opt-in hooks |
| Dream and OpenClaw | `memorymaster.bridges.dream_bridge`, `db_merge`, `delta_sync`, `qmd_bridge` | CLI/integration handlers |
| Atlas, media and actions | `memorymaster.bridges.atlas_*`, `media_*`, `action_*`, `connectors` | CLI/MCP integration handlers |
| Local search | `memorymaster.bridges.local_search` | CLI/MCP tools |
| Specialized bridges | remaining `memorymaster.bridges.*` modules | explicit integration handlers |

Dependency direction is one-way: companions may consume core contracts, while
core modules must not import companion modules. Optional behavior is installed
by importing its companion; for example, importing `wiki_engine` registers the
wiki lifecycle adapter, while importing `MemoryService` alone does not.

The supported extension seams are narrow typed provider protocols
(`LocalSearchProvider`, `TranscriptionProvider`, `OcrProvider`) plus the
read-only `WikiSimilarityCorpus` stewardship protocol. The former generic
`memorymaster.plugins` callback registry had no production consumers and was
removed in R4.1 after its deprecation window. Arbitrary validator, retrieval,
or ingestion callbacks are not a supported security boundary.

### Gradual orchestration decomposition

`MemoryService` remains the supported facade. Its Atlas/media/action persistence
API is implemented by `core.services.IntegrationService`, preserving instance
method signatures through inheritance while removing integration ownership from
the 2,617-line orchestration class. The first enforced ratchet caps
`core/service.py` at 2,450 lines; the R4.2 result is 2,205 lines. Telemetry and
lifecycle are the next extraction pair, followed by stewardship and ingestion;
policy-dense retrieval is last.

Dashboard HTTP handlers own transport only. Read-model construction lives in
`surfaces/dashboard_read_models.py`, while mutation application lives in
`surfaces/dashboard_commands.py`. Non-growth tests cap `dashboard.py` at 1,550
lines and `DashboardRequestHandler` at 720 lines; R4.2 results are 1,381 and 691.
Compatibility-shim ownership and the dated v5 removal gate are documented in
`docs/compatibility.md`.

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
  -> SQLite/Postgres returns authorized claim rows and lifecycle metadata
  -> retrieval.py ranks those rows by lexical, confidence, freshness, graph,
     and optional local/primary-store embedding signals
  -> context_optimizer.py packs query_for_context results into provider-aware budgets
```

`query_for_context` reuses the ranked rows and then chooses a text, XML, or JSON envelope that fits
the caller's token budget. R1.3 does not admit Qdrant hits into this flow: explicit or classified
claim requests use authoritative lexical fallback in local-trusted mode, prompt-context Qdrant
fallback is disconnected, and team MCP denies semantic modes. Local `hybrid` ranking is distinct
from Qdrant retrieval and operates only on rows already authorized by SQLite/Postgres.

The verbatim read path follows the same containment rule. `search_verbatim(mode="vector"|"hybrid")`
uses authoritative FTS5 and never consumes Qdrant payload text. Direct Qdrant search entry points,
including `qdrant-search` and `QdrantBackend.search`, fail closed. Qdrant upsert, sync, reconciliation,
orphan cleanup, and drift checks remain available as index maintenance. R2.1 may restore reads only
by accepting Qdrant IDs as untrusted candidates, rehydrating the canonical rows from SQLite/Postgres,
and applying the shared tenant/scope/visibility/lifecycle/sensitivity planner before ranking.

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
| File path | Responsibility | Last meaningful change |
| --- | --- | --- |
| `memorymaster/__init__.py` | Package marker and public package metadata. | `3113c90 fix: auto-ingest hook silently extracting zero claims (v3.4.1)` |
| `memorymaster/__main__.py` | `python -m memorymaster` entry point that delegates to the CLI. | `1f5571e feat: MemoryMaster v1.0.0 - Production-grade memory reliability for AI agents` |
| `memorymaster/_storage_lifecycle.py` | SQLiteStore mixin for lifecycle transitions, events, links, embeddings, and access records. | `ce4fd3c fix: 5 NameError bugs + security filter consolidation + README stats` |
| `memorymaster/_storage_read.py` | SQLiteStore mixin for claim/event/source reads and lookup helpers. | `b4c3a6b feat: bidirectional claim<->wiki binding + recall enrichment (v3.4.0)` |
| `memorymaster/_storage_schema.py` | SQLiteStore mixin for schema creation, migrations, and event integrity helpers. | `fe4dbc2 feat(atlas): Atlas Inbox V1 contract (v1.0.0 → v1.5.1, 7 commits) (#27)` |
| `memorymaster/_storage_shared.py` | Shared storage constants and helpers used by SQLite mixins. | `7e5eeff refactor: split cli.py + storage.py to enforce 800 LOC ceiling` |
| `memorymaster/_storage_sources.py` | SQLiteStore mixin for Atlas source items, evidence, and action proposals. | `fe4dbc2 feat(atlas): Atlas Inbox V1 contract (v1.0.0 → v1.5.1, 7 commits) (#27)` |
| `memorymaster/_storage_write_claims.py` | SQLiteStore mixin for creating, updating, redacting, and deleting claim payloads. | `ce4fd3c fix: 5 NameError bugs + security filter consolidation + README stats` |
| `memorymaster/access_control.py` | Role-based permission checks for agents and dashboard-like readers. | `b3cb6c1 fix: add comprehensive error handling and edge case coverage to v2.1 modules` |
| `memorymaster/action_exporters.py` | Export adapters for approved Atlas action proposals. | `30a9403 feat(atlas): WhatsApp source/evidence/action vertical slice (#20)` |
| `memorymaster/action_extractor.py` | Deterministic extraction of reviewable Atlas action candidates. | `30a9403 feat(atlas): WhatsApp source/evidence/action vertical slice (#20)` |
| `memorymaster/atlas_claim_extractor.py` | Deterministic claim extraction from Atlas evidence records. | `30a9403 feat(atlas): WhatsApp source/evidence/action vertical slice (#20)` |
| `memorymaster/atlas_contract.py` | Versioned Atlas API and CLI contract definitions. | `fe4dbc2 feat(atlas): Atlas Inbox V1 contract (v1.0.0 → v1.5.1, 7 commits) (#27)` |
| `memorymaster/auto_extractor.py` | LLM-backed extraction of structured claims from free text. | `7b049c5 chore: prepare for open-source release — scrub private data, add docs` |
| `memorymaster/auto_resolver.py` | LLM-assisted winner selection for contradictory claim pairs. | `ced8d24 fix(auto-resolver): route _llm_evaluate through llm_provider.call_llm (#33)` |
| `memorymaster/candidate_dedupe.py` | Pre-validator candidate dedupe using FTS narrowing and token overlap. | `6d7a6bb fix(v3.13.1): wire dedupe into MemoryService.run_cycle (the actual cron path) (#5)` |
| `memorymaster/claim_edges.py` | Claim-to-claim edge schema and traversal helpers. | `520131c feat(v3.11.0): F6 BM25-scaled + boost-only, F1 query_classifier, F8 shares_entity edges` |
| `memorymaster/claim_verifier.py` | Verifies codebase-sensitive claims against current files, symbols, ports, and URLs. | `1fab402 fix: audit fixes — hardcoded path, silent exceptions, WAL bypass, test regressions` |
| `memorymaster/cli.py` | CLI parser and top-level command dispatch setup. | `3b5c3f4 feat(entity-graph): DOT/GraphML export CLI for Gephi/yEd/Cytoscape (#60)` |
| `memorymaster/cli_handlers_basic.py` | CLI handlers for core claim, query, lifecycle, snapshot, metrics, and ops commands. | `3b5c3f4 feat(entity-graph): DOT/GraphML export CLI for Gephi/yEd/Cytoscape (#60)` |
| `memorymaster/cli_handlers_curation.py` | CLI handlers for wiki, vault, dream, Atlas, and command dispatch registration. | `fe4dbc2 feat(atlas): Atlas Inbox V1 contract (v1.0.0 → v1.5.1, 7 commits) (#27)` |
| `memorymaster/cli_helpers.py` | Shared CLI parsing and output helpers. | `fe4dbc2 feat(atlas): Atlas Inbox V1 contract (v1.0.0 → v1.5.1, 7 commits) (#27)` |
| `memorymaster/closets.py` | BM25-friendly wiki pointer index used as an optional recall boost. | `520131c feat(v3.11.0): F6 BM25-scaled + boost-only, F1 query_classifier, F8 shares_entity edges` |
| `memorymaster/config.py` | Environment and file-backed tunables for retrieval, validation, decay, and confidence. | `f074a53 feat(config): apply 90-day calibration priors to default confidence by type (#55)` |
| `memorymaster/conflict_resolver.py` | Deterministic conflict resolution and supersession logic. | `dd4dd6c refactor: simplify _pick_winner logic to reduce complexity (C901)` |
| `memorymaster/context_hook.py` | Claude Code hook-facing recall and observe operations. | `2598a92 fix(observe): default scope auto-derives from cwd, not literal 'project' (F-5) (#12)` |
| `memorymaster/context_optimizer.py` | Provider-aware packing for `query_for_context` token budgets. | `88c27f7 feat(context-optimizer): provider-aware chunk packing for query_for_context (#54)` |
| `memorymaster/daily_notes.py` | Session daily-note generation and ghost-topic detection. | `5783b74 feat: daily notes + ghost note detection (second brain pattern)` |
| `memorymaster/dashboard.py` | Built-in HTTP dashboard, API endpoints, SSE stream, metrics, and observability views. | `5a3fbd0 feat(dashboard): /api/v1/review-queue mobile-friendly candidate queue (#59)` |
| `memorymaster/db_merge.py` | Bidirectional import and merge logic for other MemoryMaster databases. | `9c3bc0f test(db-merge): add coverage for OpenClaw bidirectional sync edge cases (#56)` |
| `memorymaster/dream_bridge.py` | Sync bridge between MemoryMaster claims and Claude Auto Dream files. | `ce4fd3c fix: 5 NameError bugs + security filter consolidation + README stats` |
| `memorymaster/embeddings.py` | Embedding provider abstraction and concrete provider calls. | `ea99b73 fix: gemini embedding 404 + dashboard test port skip (#119, #123)` |
| `memorymaster/entity_extractor.py` | Regex-first and optional LLM entity extraction from claim text. | `d4702f5 feat(v3.9.0): steal everything good — 9 features ported from 6 memory tools` |
| `memorymaster/entity_graph.py` | Entity and relationship extraction/storage for the knowledge graph. | `7b049c5 chore: prepare for open-source release — scrub private data, add docs` |
| `memorymaster/entity_registry.py` | Canonical entity registry with alias normalization. | `6d7a737 fix(entity-registry): record every original_form variant, not just the first` |
| `memorymaster/federated_graphify.py` | Cross-project graph discovery and merged graph queries. | `d4702f5 feat(v3.9.0): steal everything good — 9 features ported from 6 memory tools` |
| `memorymaster/feedback.py` | Usage feedback capture and quality-score inputs. | `b7a212e refactor: extract score computation from compute_quality_scores` |
| `memorymaster/graph_store.py` | Kuzu-backed graph retrieval stream. | `eaa8e8f feat(recall): graph score 1/(1+hops) — distance-weighted (12.1)` |
| `memorymaster/hook_log.py` | Shared structured log helper for installed hook observability. | `0ffc672 feat(hooks): shared log_hook helper for observability (#118)` |
| `memorymaster/jobs/__init__.py` | Jobs package marker. | `ba07072 feat: implement 25-feature improvement plan` |
| `memorymaster/jobs/calibration.py` | Computes confidence-prior reports from validator event history. | `f3db435 feat(calibration): 90-day confidence-prior recalibration job (#50)` |
| `memorymaster/jobs/compact_summaries.py` | Clusters archived claims and creates LLM summary claims with source links. | `ed1f34f refactor: extract embedding cluster assignment logic` |
| `memorymaster/jobs/compactor.py` | Archives old or inactive claims according to retention policy. | `b5a0a79 fix(compactor): distinguish scope=None from scope='project' (F-9) (#17)` |
| `memorymaster/jobs/decay.py` | Applies confidence decay and stale transitions over time. | `d0dd629 fix(decay): record event when claim has future updated_at (F-10) (#18)` |
| `memorymaster/jobs/dedup.py` | Detects duplicate claims and merges or supersedes them. | `5ea2f75 feat(dedup): add --limit and --scope flags for incremental dedup runs (#36)` |
| `memorymaster/jobs/deterministic.py` | Deterministic validators for paths, identifiers, URLs, and other probeable facts. | `8ac7941 refactor: use data-driven approach for predicate validation` |
| `memorymaster/jobs/entity_graph_export.py` | Exports entity graph nodes and edges as DOT or GraphML. | `3b5c3f4 feat(entity-graph): DOT/GraphML export CLI for Gephi/yEd/Cytoscape (#60)` |
| `memorymaster/jobs/extractor.py` | Normalizes and extracts claim candidates for the steward pipeline. | `08c4bcd fix: stop assigning generic (workspace,path) tuples that cause mass conflicts` |
| `memorymaster/jobs/staleness.py` | Detects claims whose cited source files changed since citation. | `ba07072 feat: implement 25-feature improvement plan` |
| `memorymaster/jobs/validator.py` | Scores validation evidence and promotes or demotes claims. | `289c954 feat(steward): calibrated classifier for promotion gate (#129)` |
| `memorymaster/key_rotator.py` | Rotates LLM API keys and tracks cooldowns after rate limits. | `12d3a53 feat: multi-key Gemini rotator + stable default model + 429 body logging` |
| `memorymaster/lifecycle.py` | Claim lifecycle transition rules and state checks. | `bf25482 feat(dashboard): claim lineage view via /claim/<id>/lineage (#46)` |
| `memorymaster/llm_provider.py` | Unified Google, OpenAI, Anthropic, Ollama, and Claude CLI client. | `1472d71 fix(llm-provider): wire KeyRotator into _call_google for multi-key rotation (#38)` |
| `memorymaster/llm_steward.py` | Automated claim extraction, validation, dedupe, and steward proposals. | `6a24b3e fix(llm_steward): shadow mode treats would_archive as terminal (F-8) (#13)` |
| `memorymaster/mcp_server.py` | FastMCP server, tool input validation, sensitivity guard, and MCP tool definitions. | `a38004b feat(mcp): query_meta_decisions tool for cross-project decision aggregation (#48)` |
| `memorymaster/mcp_usage.py` | Records and queries MCP tool usage windows. | `8b7aea7 feat(mcp): usage tracking table + mcp-usage-report CLI for billing (#58)` |
| `memorymaster/media_processing.py` | Atlas media processing protocols and evidence result types. | `30a9403 feat(atlas): WhatsApp source/evidence/action vertical slice (#20)` |
| `memorymaster/media_providers.py` | Real Atlas transcription and OCR provider adapters. | `fe4dbc2 feat(atlas): Atlas Inbox V1 contract (v1.0.0 → v1.5.1, 7 commits) (#27)` |
| `memorymaster/metrics_exporter.py` | Prometheus-style metrics export helpers. | `ce4fd3c fix: 5 NameError bugs + security filter consolidation + README stats` |
| `memorymaster/models.py` | Domain model dataclasses and validation helpers. | `fe4dbc2 feat(atlas): Atlas Inbox V1 contract (v1.0.0 → v1.5.1, 7 commits) (#27)` |
| `memorymaster/operator.py` | Operator loop support for reviewing and applying queued memory work. | `886bec4 fix: green CI + v3.2.0 release prep + repo cleanup` |
| `memorymaster/operator_queue.py` | WAL-backed durable queue for pending operator turns. | `761eabc refactor: extract migration helpers to reduce migrate_from_json complexity (11→8)` |
| `memorymaster/policy.py` | Policy-mode configuration and cadence override helpers. | `0dff74a feat(policy): MEMORYMASTER_POLICY_MODE env-var opt-in for cadence` |
| `memorymaster/postgres_store.py` | Postgres storage backend with parity methods for the service layer. | `e337c07 chore(storage): audit SQLite/Postgres parity, add 3 missing pg methods (#35)` |
| `memorymaster/qdrant_backend.py` | Qdrant maintenance-index backend; payload search fails closed while upsert/sync/reconcile count/ID operations remain available. | `7b049c5 chore: prepare for open-source release — scrub private data, add docs` |
| `memorymaster/qdrant_recall_fallback.py` | Compatibility helpers for the disconnected prompt-context fallback; activation knobs cannot enable reads during R1.3. | `a1e6786 feat(recall): Qdrant vector-search fallback for sparse-candidate prompts` |
| `memorymaster/qmd_bridge.py` | Conversion bridge between OpenClaw QMD records and MemoryMaster claims. | `a4b5dcc feat: QMD ↔ memorymaster bridge for OpenClaw integration` |
| `memorymaster/query_classifier.py` | Rule-based routing of queries to retrieval modes. | `265d951 refactor: inline citation locator/excerpt, merge print_claim header+text` |
| `memorymaster/query_expansion.py` | Entity alias and synonym expansion for recall queries. | `ac071de feat(recall): query expansion via entity-matched synonyms (roadmap 1.5)` |
| `memorymaster/recall_fusion.py` | Reciprocal-rank fusion for multi-stream recall results. | `0e133fe feat(recall): add RRF fusion as opt-in retrieval ranker` |
| `memorymaster/recall_tokenizer.py` | Token extraction and normalization for FTS recall. | `bb71944 feat(recall): tokenizer v2 — df=0 penalty + stem + synonym recovery` |
| `memorymaster/retrieval.py` | Ranking and scoring for claim query results. | `c425a63 refactor: extract score computation from rank_claim_rows` |
| `memorymaster/retry.py` | Retry and backoff helpers for transient connection failures. | `ba07072 feat: implement 25-feature improvement plan` |
| `memorymaster/review.py` | Review queue item model and prioritization helpers. | `1f5571e feat: MemoryMaster v1.0.0 - Production-grade memory reliability for AI agents` |
| `memorymaster/rl_trainer.py` | Trains a quality model from feedback history. | `21ed10f fix: add explicit strict= parameter to zip calls (B905) - 5 fixes` |
| `memorymaster/scheduler.py` | Periodic daemon loop for cycles, compaction, and scheduled maintenance. | `ba07072 feat: implement 25-feature improvement plan` |
| `memorymaster/schema.py` | Loads bundled SQL schema text. | `1f5571e feat: MemoryMaster v1.0.0 - Production-grade memory reliability for AI agents` |
| `memorymaster/scope_utils.py` | Derives project scopes from cwd and transcript metadata. | `d4702f5 feat(v3.9.0): steal everything good — 9 features ported from 6 memory tools` |
| `memorymaster/security.py` | Canonical redaction and sensitivity-pattern filter. | `6b72cf8 feat(security): cover v2 adversarial traps (private_ip:port, home paths, PAN, DSN shapes)` |
| `memorymaster/service.py` | Main MemoryService facade for ingest, query, cycles, context, Atlas, and exports. | `88c27f7 feat(context-optimizer): provider-aware chunk packing for query_for_context (#54)` |
| `memorymaster/session_tracker.py` | SQLite-backed agent session tracking. | `b3cb6c1 fix: add comprehensive error handling and edge case coverage to v2.1 modules` |
| `memorymaster/setup_hooks.py` | Interactive installer for hooks, MCP config, cron, and agent docs. | `c457f78 fix: follow-up fixes from second code review` |
| `memorymaster/skill_evolver.py` | Extracts procedural lessons from feedback and session patterns. | `7b049c5 chore: prepare for open-source release — scrub private data, add docs` |
| `memorymaster/snapshot.py` | SQLite snapshot, rollback, and diff support using git-backed metadata. | `9c3b551 fix: replace try-except-pass with contextlib.suppress (SIM105) - 12 fixes` |
| `memorymaster/steward.py` | Steward proposal discovery, approval, and reporting workflow. | `0267f43 refactor: extract proposal finding and approval logic in steward` |
| `memorymaster/steward_classifier.py` | Runtime loader for the calibrated promotion classifier artifact. | `841d385 fix(ci): lazy-import numpy so package imports without [ml] extra (#2)` |
| `memorymaster/steward_features.py` | Feature extraction for the steward classifier. | `841d385 fix(ci): lazy-import numpy so package imports without [ml] extra (#2)` |
| `memorymaster/storage.py` | SQLiteStore composition root that combines storage mixins. | `30a9403 feat(atlas): WhatsApp source/evidence/action vertical slice (#20)` |
| `memorymaster/store_factory.py` | Chooses SQLite or Postgres store from a database path or DSN. | `1f5571e feat: MemoryMaster v1.0.0 - Production-grade memory reliability for AI agents` |
| `memorymaster/transcript_miner.py` | Parses Claude transcript JSONL and ingests mined claims. | `ce4fd3c fix: 5 NameError bugs + security filter consolidation + README stats` |
| `memorymaster/turn_schema.py` | Normalized turn model for transcript processing. | `1f5571e feat: MemoryMaster v1.0.0 - Production-grade memory reliability for AI agents` |
| `memorymaster/vault_bases.py` | Generates Obsidian Bases views from wiki frontmatter. | `ce4fd3c fix: 5 NameError bugs + security filter consolidation + README stats` |
| `memorymaster/vault_curator.py` | LLM-assisted organization of claims into an Obsidian vault hierarchy. | `ce4fd3c fix: 5 NameError bugs + security filter consolidation + README stats` |
| `memorymaster/vault_exporter.py` | Exports claims as linked Markdown files for Obsidian. | `c804cbe feat: incremental vault export + 4 MCP tools for entities/feedback` |
| `memorymaster/vault_linter.py` | Audits vault articles for contradictions, orphaned material, gaps, and stale content. | `702c904 feat(wiki): wiki-freshness CLI + STALE_ARTICLE lint (11.8, Option A)` |
| `memorymaster/vault_log.py` | Append-only log of wiki and vault operations. | `88d7afb feat: LLM Wiki architecture — lint, log, synthesis, query capture` |
| `memorymaster/vault_query_capture.py` | Saves high-value query answers as new wiki pages. | `88d7afb feat: LLM Wiki architecture — lint, log, synthesis, query capture` |
| `memorymaster/vault_synthesis.py` | Updates related wiki pages when new claims arrive. | `88d7afb feat: LLM Wiki architecture — lint, log, synthesis, query capture` |
| `memorymaster/verbatim_recall.py` | Optional raw-conversation FTS recall stream. | `6e120a2 feat(recall): MemPalace-style verbatim retrieval stream (opt-in)` |
| `memorymaster/verbatim_store.py` | Raw conversation storage with authoritative FTS5 reads; Qdrant sync remains, while vector/hybrid reads downgrade to FTS5. | `89c900d fix(verbatim-store): use full-content hash + point IDs for Qdrant dedup (#43)` |
| `memorymaster/webhook.py` | Webhook notification helper for claim events. | `b3cb6c1 fix: add comprehensive error handling and edge case coverage to v2.1 modules` |
| `memorymaster/wiki_engine.py` | Absorbs claims into compiled wiki articles and related cleanup/breakdown flows. | `bf25482 feat(dashboard): claim lineage view via /claim/<id>/lineage (#46)` |
| `memorymaster/wiki_freshness.py` | Computes freshness scores from wiki article absorb dates. | `702c904 feat(wiki): wiki-freshness CLI + STALE_ARTICLE lint (11.8, Option A)` |
| `memorymaster/wiki_similarity.py` | Computes claim-to-wiki semantic similarity for steward features. | `795c781 feat(steward/v3): WikiCorpus multi-scope — close chrono gap for 11.5` |
| `memorymaster/wiki_suggest.py` | Suggests existing wiki links from text using entity graph proximity. | `c1b3891 feat(wiki): wiki-suggest-links CLI for Obsidian autocomplete via entity graph (#51)` |
| `memorymaster/wiki_validate.py` | Validates and optionally fixes wiki article frontmatter and wikilink hygiene. | `d4702f5 feat(v3.9.0): steal everything good — 9 features ported from 6 memory tools` |
<!-- module-map:end -->
