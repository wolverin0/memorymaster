# Codebase Structure

**Analysis Date:** 2026-08-17
**Analysed commit:** `c832df4` (main), package version `4.7.6`

## Directory Layout

```
memorymaster/                     # repo root
├── memorymaster/                 # the package (see table below)
├── tests/                        # 401 test modules + fixtures/, integration/, soak/
├── scripts/                      # 73 operational / backfill / benchmark scripts
├── docs/                         # handbook, env reference, ADRs, specs, generated/
├── integrations/                 # hermes-memorymaster companion plugin
├── helm/memorymaster/            # Chart.yaml, values.yaml, deployment/service/pvc
├── benchmark/, benchmarks/       # retrieval + LongMemEval benchmark harnesses
├── artifacts/, reports/, debates/# dated evaluation outputs (committed, append-only)
├── pyproject.toml                # deps, console scripts, ruff config
├── pytest.ini                    # markers (ml/postgres/soak/unit/calibration), timeout=600
├── conftest.py                   # root test fixtures
├── docker-compose.yml, docker-compose.postgres.yml, Dockerfile
└── AGENTS.md / CLAUDE.md / GEMINI.md / DOCS-MAP.md
```

```
memorymaster/
├── core/            # domain + MemoryService facade      (26 .py)
│   └── services/    # extracted bounded services (integration.py)
├── stores/          # SQLite + Postgres persistence       (36 .py)
│   └── migrations/  # 0001..0021 versioned migrations + runner.py
├── recall/          # retrieval, ranking, fusion, hook     (21 .py)
├── govern/          # steward, resolvers, scheduler        (40 .py)
│   └── jobs/        # one module per steward cycle phase
├── surfaces/        # CLI, MCP, dashboard, operator, setup (31 .py)
├── knowledge/       # wiki, vault, entities, rules, graph  (32 .py)
├── bridges/         # Atlas, dream, media, sync, connectors(22 .py)
│   ├── connectors/  # whatsapp.py
│   └── local_search/# Everything-backed local file search
├── capture/         # governed capture adapters + worker    (7 .py)
├── dreaming/        # background consolidation worker       (7 .py)
├── profile/         # compiled user profile engine          (7 .py)
├── evaluation/      # judges, benchmarks, provenance        (9 .py)
├── operations/      # read-only operational review          (2 .py)
├── public/          # stable v1 API surface (v1.py, demo.py) (3 .py)
├── config_templates/# hook templates installed by setup     (11 hooks)
├── connectors/ jobs/ migrations/   # deprecated package shims
├── *.py (109 files) # deprecated module shims → subpackages
├── schema.sql / schema_postgres.sql
```

## Directory Purposes

| Package | Purpose | Notable modules |
|---------|---------|-----------------|
| `core/` | Domain models, service facade, security, policy, config | `service.py` (MemoryService), `models.py`, `security.py`, `intake_policy.py`, `temporal_policy.py`, `llm_budget.py`, `llm_provider.py`, `access_control.py`, `observability.py`, `spool.py` |
| `core/services/` | Bounded services extracted from the facade | `integration.py` (Atlas source items, action proposals) |
| `stores/` | Persistence | `storage.py` (SQLiteStore facade), `_storage_schema.py`, `_storage_read.py`, `_storage_write_claims.py`, `_storage_lifecycle.py`, `_storage_sources.py`, `_storage_pagination.py`, `postgres_store.py`, `store_factory.py`, `claim_identity.py`, `snapshot.py` |
| `stores/migrations/` | Versioned schema evolution | `runner.py` (checksum + drift check), `0001_initial.py` … `0021_compiled_user_profile.py` |
| `recall/` | Getting the right claims back | `context_hook.py` (prompt hook), `retrieval.py` (ranking), `recall_fusion.py` (RRF), `planner.py`, `query_classifier.py`, `query_expansion.py`, `query_cache.py`, `embeddings.py`, `qdrant_backend.py`/`qdrant_outbox.py`/`qdrant_transport.py`, `graph_store.py`, `verbatim_recall.py`, `local_rerank.py`, `llm_rerank.py`, `context_optimizer.py` |
| `govern/` | Keeping memory true over time | `steward.py` (probes), `llm_steward.py`, `scheduler.py`, `auto_resolver.py`, `conflict_resolver.py`, `candidate_dedupe.py`, `claim_verifier.py`, `contradiction_probe.py`, `ingest_governance.py`, `feedback.py`, `recovery.py`, `privacy_ops.py` |
| `govern/jobs/` | Steward cycle phases, each exposing `run(store, …) -> dict` | `validator.py`, `decay.py`, `dedup.py`, `deterministic.py`, `extractor.py`, `compactor.py`, `compact_summaries.py`, `integrity.py`, `qdrant_reconcile.py`, `spool_drain.py`, `staleness.py`, `fk_repair.py`, `scheduled_archive.py`, `calibration.py` |
| `surfaces/` | Everything an agent or human touches | `mcp_server.py` (50 tools), `mcp_http.py`, `cli.py` + `cli_handlers_{basic,curation,integrity,public,skills}.py`, `dashboard.py` + `dashboard_read_models.py`/`dashboard_commands.py`, `operator.py`, `operator_queue.py`, `metrics_exporter.py`, `setup_hooks.py`, `session_end_ingest.py`, `capture_inbox.py`, `dreaming_cli.py` |
| `knowledge/` | Compiled human-readable knowledge | `wiki_engine.py`, `vault_linter.py`, `vault_curator.py`, `vault_exporter.py`, `entity_registry.py`, `entity_extractor.py`, `entity_graph.py`, `rule_miner.py`, `rules.py`, `skills.py`, `graph_observation_engine.py`, `graph_observation_repository.py`, `ontology.py`, `context_bundle.py` |
| `bridges/` | External systems | `dream_bridge.py`, `atlas_claim_extractor.py`, `atlas_llm_extractor.py`, `atlas_contract.py`, `db_merge.py`, `delta_sync.py`, `media_providers.py`, `media_processing.py`, `qmd_bridge.py`, `federated_graphify.py`, `connectors/whatsapp.py`, `local_search/*` |
| `capture/` | Governed ambient capture | `adapters.py`, `producers.py`, `repository.py`, `worker.py`, `coverage.py` |
| `dreaming/` | Candidate-first background consolidation | `worker.py`, `ledger.py`, `providers.py`, `evaluation.py`, `capture.py` |
| `profile/` | Weekly map/reduce compiled user profile | `engine.py`, `repository.py`, `renderer.py`, `providers.py` |
| `evaluation/` | Offline evaluation only, never in the hot path | `opencode_judge.py`, `graph_observation_evaluator.py`, `paper_research.py`, `skill_outcomes.py`, `temporal_projection.py`, `budget_policy.py` |
| `public/` | Friendly stable API (`remember`/`recall`/`forget`/`improve`) | `v1.py`, `demo.py` |
| `config_templates/hooks/` | Hook scripts the installer copies into agent configs | `memorymaster-recall.py`, `-session-start.py`, `-session-end.py`, `-steward-cycle.py`, `-classify.py`, `-auto-ingest.py`, `-dream-capture.py`, `-precompact.py`, `-validate-wiki.py` |

## Key File Locations

**Entry points** (declared in `pyproject.toml [project.scripts]`):
- `memorymaster/surfaces/cli.py:main` — the `memorymaster` CLI (also `memorymaster/__main__.py`)
- `memorymaster/surfaces/mcp_server.py:main` — stdio MCP server
- `memorymaster/surfaces/mcp_http.py:main` — authenticated HTTP MCP
- `memorymaster/surfaces/dashboard.py:main`, `govern/llm_steward.py:main`,
  `surfaces/setup_hooks.py:main`, `surfaces/session_end_ingest.py:main`,
  `surfaces/operations.py:main`

**Configuration:**
- `memorymaster/core/config.py` — every tunable constant + its env var
- `docs/env-reference.md` — env var catalogue
- `pytest.ini`, `pyproject.toml` (`[tool.ruff]`)

**Core logic:**
- `memorymaster/core/service.py` — ingest (`:521`), run_cycle (`:845`), query_rows (`:1381`)
- `memorymaster/stores/storage.py` — `SQLiteStore` mixin composition (`:85`)
- `memorymaster/recall/context_hook.py` — production recall (`recall` at `:1095`)

**Schema:**
- `memorymaster/schema.sql` (11 tables) / `memorymaster/schema_postgres.sql` (13 tables)
- `memorymaster/stores/_storage_schema.py` — ~19 idempotent `_ensure_*` upgrade passes
- `memorymaster/stores/migrations/` — 20 versioned migrations, numbered 0001–0021 (0005 absent)

**Testing:**
- `tests/test_*.py` — 401 modules, flat
- `tests/fixtures/` — eval datasets (`qrels_search.json`, `classify_eval.jsonl`, `atlas/`)
- `tests/integration/`, `tests/soak/` (`chaos_soak.py`, `soak_writers.py`, `soak_slice.py`)

## Naming Conventions

**Files:**
- Modules: `snake_case.py`. Private implementation mixins carry a leading underscore and a
  package prefix: `stores/_storage_read.py`, `govern/jobs/_sensitivity_scan.py`.
- Migrations: `NNNN_snake_case.py`, enforced by
  `^(\d{4})_[a-z0-9_]+\.py$` (`memorymaster/stores/migrations/runner.py:26`).
- Hook templates: `memorymaster-<event>.py` (`memorymaster/config_templates/hooks/`).
- Tests: `tests/test_<subject>.py`; dated evaluation output: `artifacts/YYYY-MM-DD-<slug>.<ext>`.

**Code:**
- Classes `PascalCase`; mixins `_XxxMixin`; steward phases are modules with a `run()` function.
- CLI subcommands are kebab-case (`run-cycle`, `ingest-daydream`, `label-source-item`).
- MCP tools are snake_case (`query_memory`, `ingest_claim`, `query_for_task`).
- Env flags: `MEMORYMASTER_<FEATURE>`; new behaviour ships default-OFF.

**Directories:** single-word lowercase package names describing a layer (`core`, `stores`,
`recall`, `govern`, `surfaces`, `bridges`, `knowledge`).

## Where to Add New Code

| Kind of change | Put it in | Tests |
|---|---|---|
| Domain rule, claim field, policy | `memorymaster/core/` (+ `models.py`) | `tests/test_<feature>.py` |
| SQL / persistence | the matching `stores/_storage_*.py` mixin, **plus** `stores/postgres_store.py` parity | `tests/test_postgres_*_boundary.py` |
| Schema change | new `stores/migrations/00NN_<name>.py` **and** `schema.sql` + `schema_postgres.sql` | migration test |
| Ranking / retrieval stream | `memorymaster/recall/` (feed it into `recall_fusion` as a stream) | `tests/test_fts5_search.py`, `test_vector_search.py` |
| New steward phase | `memorymaster/govern/jobs/<name>.py` exposing `run(store, …) -> dict`, wired into `core/service.py:run_cycle` inside its own try/except | phase test |
| New MCP tool | `memorymaster/surfaces/mcp_server.py` via `AuthorizedFastMCP` (authorization metadata required, `:834`) | `tests/test_mcp_authorization_boundary.py` |
| New CLI command | `surfaces/cli.py` parser + a handler in `surfaces/cli_handlers_*.py` | CLI test |
| Wiki / entity / rules work | `memorymaster/knowledge/` | — |
| External system integration | `memorymaster/bridges/` (leaf modules only; core must not import them) | `tests/test_extension_boundaries.py` |
| Shared helper | the owning subpackage — **never** a new root-level `memorymaster/*.py` | — |

## Enforced Size and Architecture Budgets

`tests/test_architecture_budgets.py` fails the build on regrowth. Current values measured
2026-08-17:

| Target | Budget | Current | Source |
|---|---|---|---|
| `memorymaster/core/service.py` | ≤ 2450 lines | **2450** (at cap) | `test_architecture_budgets.py:23` |
| `memorymaster/surfaces/dashboard.py` | ≤ 1550 lines | **1550** (at cap) | `:24` |
| `DashboardRequestHandler` class | ≤ 720 lines | not measured | `:25` |
| `core/services/integration.py` | ≤ 800 lines | 269 | `:26-31` |
| `surfaces/dashboard_read_models.py` | ≤ 800 lines | 235 | `:26-31` |
| `surfaces/dashboard_commands.py` | ≤ 800 lines | 70 | `:26-31` |
| Any function in those three files | ≤ 50 lines | — | `:34-44` |

Also pinned by the same file:
- `MemoryService` must subclass `IntegrationService`, and `upsert_source_item` /
  `create_action_proposal` must live on `IntegrationService`, not on `MemoryService` (`:47-55`).
- `dashboard.py` must import from `dashboard_read_models` and `dashboard_commands` (`:58-62`).
- `docs/compatibility.md` must keep a dated removal gate (`2026-09-30`) naming
  `memorymaster.service` (`:64-68`).

Complementary boundary pins live in `tests/test_extension_boundaries.py`: `core`, `govern`,
`recall`, `stores` may not import `memorymaster.bridges`, `memorymaster.knowledge.wiki`, or
`memorymaster.knowledge.vault`, and importing `core.service` must not load or register them.

**Two budgets are exactly at their cap.** Any line added to `core/service.py` or
`surfaces/dashboard.py` breaks the suite — extract into a bounded service or a
`cli_handlers_*`/`dashboard_*` module instead of editing in place.

## Special Directories

| Directory | Contents | Generated | Committed |
|---|---|---|---|
| `artifacts/` | Dated evaluation runs, audit HTML, JSONL sweeps | Yes (by scripts) | Yes |
| `reports/`, `debates/`, `benchmark(s)/` | Benchmark and review output | Yes | Yes |
| `docs/generated/` | Generated docs | Yes | Yes |
| `memorymaster/config_templates/` | Hook + agent-config templates copied by `setup_hooks.py` | No | Yes |
| `helm/memorymaster/` | Deployment chart (Chart/values/deployment/service/pvc) | No | Yes |
| `integrations/hermes-memorymaster/` | Companion plugin package | No | Yes |
| `*.db`, `.tmp_pytest/`, `.pytest_cache/` | Local DBs and test scratch | Yes | No (`.gitignore`) |

## Deprecated Paths (do not add to)

- 109 root-level `memorymaster/*.py` shims rebind `sys.modules` to the canonical subpackage
  (pattern: `memorymaster/service.py:1-11`).
- Package shims: `memorymaster/jobs/` → `govern/jobs/`, `memorymaster/migrations/` →
  `stores/migrations/`, `memorymaster/connectors/` → `bridges/connectors/`.
- Earliest removal 2026-09-30, and only in v5.0+ (`docs/compatibility.md`).
- `docs/architecture.md` still describes the pre-restructure flat layout (`service.py`,
  `jobs/…` at root) — stale; prefer this document and the subpackage `__init__.py` docstrings.

---

*Structure analysis: 2026-08-17*
