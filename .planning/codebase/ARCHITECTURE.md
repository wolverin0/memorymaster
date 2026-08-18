<!-- refreshed: 2026-08-17 -->
# Architecture

**Analysis Date:** 2026-08-17
**Analysed commit:** `c832df4` (main), package version `4.7.6` (`pyproject.toml:7`)

## System Overview

```text
┌─────────────────────────────────────────────────────────────┐
│  Surfaces — agent-facing entry points  `memorymaster/surfaces/` │
├──────────────────┬──────────────────┬───────────────────────┤
│  MCP server      │  CLI             │  Dashboard / operator  │
│ `surfaces/mcp_   │ `surfaces/cli.py`│ `surfaces/dashboard.py`│
│  server.py`      │ + cli_handlers_* │ + operator.py          │
└────────┬─────────┴────────┬─────────┴──────────┬────────────┘
         │                  │                     │
         ▼                  ▼                     ▼
┌─────────────────────────────────────────────────────────────┐
│  Core domain — `memorymaster/core/`                          │
│  MemoryService facade (`core/service.py:358`), models,       │
│  security/sanitisation, intake policy, config, llm_budget    │
└───┬───────────────────┬───────────────────┬─────────────────┘
    │                   │                   │
    ▼                   ▼                   ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────────────────┐
│ Recall       │ │ Govern       │ │ Knowledge / Bridges       │
│ `recall/`    │ │ `govern/`    │ │ `knowledge/`, `bridges/`  │
│ ranking,     │ │ steward,     │ │ wiki, entities, rules;    │
│ fusion, hook │ │ jobs, LLM    │ │ Atlas, dream, media, sync │
└──────┬───────┘ └──────┬───────┘ └──────────┬───────────────┘
       │                │                     │
       ▼                ▼                     ▼
┌─────────────────────────────────────────────────────────────┐
│  Stores — `memorymaster/stores/`                             │
│  SQLiteStore (`stores/storage.py:85`) ← PostgresStore        │
│  (`stores/postgres_store.py:158`), routed by `store_factory` │
└───────────────────────────┬─────────────────────────────────┘
                            ▼
      SQLite file (FTS5 + WAL)  |  Postgres DSN  |  Qdrant (optional)
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| `MemoryService` | Public facade: ingest, query/retrieve, run_cycle, lifecycle ops | `memorymaster/core/service.py:358` |
| `IntegrationService` | Extracted base: Atlas source items, action proposals, media | `memorymaster/core/services/integration.py` |
| `SQLiteStore` | Persistence facade composed from six `_storage_*` mixins | `memorymaster/stores/storage.py:85` |
| `PostgresStore` | Team/multi-tenant backend, **subclasses `SQLiteStore`** | `memorymaster/stores/postgres_store.py:158` |
| `create_store` | DSN-based backend routing + read-only wiring | `memorymaster/stores/store_factory.py:14` |
| `context_hook.recall` | Production per-prompt recall hook (multi-stream, read-only) | `memorymaster/recall/context_hook.py:1095` |
| `rrf_fuse` | Reciprocal Rank Fusion across retrieval streams | `memorymaster/recall/recall_fusion.py:48` |
| `rank_claim_rows` | Lexical/vector/freshness/tier scoring for hybrid mode | `memorymaster/recall/retrieval.py:507` |
| `build_retrieval_plan` | Normalises query + status filters into a plan | `memorymaster/recall/planner.py:106` |
| `govern.jobs.*` | Steward cycle phases (validator, decay, dedup, integrity…) | `memorymaster/govern/jobs/` |
| `steward` / `llm_steward` | Deterministic probes + LLM adjudication of claims | `memorymaster/govern/steward.py`, `govern/llm_steward.py` |
| MCP server | 50 registered tools over FastMCP stdio/HTTP | `memorymaster/surfaces/mcp_server.py:853` |

## Pattern Overview

**Overall:** layered package with a single service facade over a mixin-composed store, plus
opt-in companion layers (knowledge, bridges) that the core is forbidden to import.

**Key Characteristics:**
- One facade class (`MemoryService`) is the only sanctioned write path; stores are never
  called directly from surfaces.
- Every subpackage carries a docstring stating its layer role (e.g. `core/__init__.py`,
  `surfaces/__init__.py`) — treat those as the layering contract.
- 109 root-level `memorymaster/*.py` modules are **deprecated shims** that rebind
  `sys.modules` to the canonical subpackage (`memorymaster/service.py:1-11`).
- Feature work lands behind default-OFF env flags; the "flag off = byte-identical legacy
  behaviour" promise is stated in-code repeatedly (e.g. `core/service.py:57-62`).

## Layers

**`core/` — domain and facade (26 modules)**
- Purpose: models, service facade, security/sanitisation, intake policy, config, LLM budget.
- Depends on: `stores`, `recall`, `govern` (imports at `core/service.py:11-50`).
- Used by: every other layer. Highest fan-in in the package.

**`stores/` — persistence (36 modules)**
- Purpose: SQLite facade + mixins, Postgres parity backend, snapshots, versioned migrations.
- Depends on: nothing above it. `SQLiteStore` mixes `_SchemaMixin`, `_ReadMixin`,
  `_WriteClaimsMixin`, `_LifecycleMixin`, `_SourceItemsMixin`, `_PaginationMixin`
  (`stores/storage.py:85-91`).

**`recall/` — retrieval (21 modules)**
- Purpose: candidate gathering (FTS5/vector/graph/verbatim), ranking, RRF fusion, query
  classification/expansion/caching, embeddings, Qdrant transport, the recall hook.

**`govern/` — governance (40 modules incl. `govern/jobs/`)**
- Purpose: steward cycle phases, LLM steward, conflict/auto resolvers, candidate dedupe,
  feedback/quality scoring, scheduler daemon.

**`surfaces/` — user-facing (31 modules)**
- Purpose: CLI, MCP (stdio + HTTP), dashboard, operator queue, metrics exporter, setup/hooks.
- Contract: "everything imports INTO surfaces, nothing imports FROM it"
  (`surfaces/__init__.py:1-7`). One live exception — a lazy import of
  `surfaces.session_tracker` at `core/service.py:1796`.

**`knowledge/` (32) and `bridges/` (22) — optional companions**
- `knowledge/`: wiki engine, vault linter/curator/exporter, entity registry/graph, rule
  miner, graph observations, skills.
- `bridges/`: dream bridge, Atlas extractors, DB merge/delta sync, media providers,
  WhatsApp connector, local filesystem search.
- Enforced boundary: `core`, `govern`, `recall`, `stores` must not import
  `memorymaster.bridges`, `memorymaster.knowledge.wiki`, `memorymaster.knowledge.vault`
  (`tests/test_extension_boundaries.py:15-41`).

**Feature packages:** `capture/` (governed capture adapters + worker), `dreaming/`
(background consolidation worker + ledger), `profile/` (compiled user profile engine),
`evaluation/` (judges, benchmarks), `public/` (stable v1 API), `operations/` (read-only ops).

## Data Flow

### Ingest path

1. Surface calls `svc.ingest(...)` — MCP tool `ingest_claim` / `remember`, CLI `ingest`,
   or a hook (`memorymaster/core/service.py:521`).
2. `sanitize_claim_input` runs the **sensitivity filter** and returns findings
   (`core/service.py:550`, implemented in `core/security.py`).
3. Identity resolution + `validate_persisted_metadata` for source agent / tenant
   (`core/service.py:579-586`).
4. Bitemporal guard `validate_temporal_fields`, plus the single-bound `valid_until`
   backdate fix (`core/service.py:592-607`).
5. `ingest_governance.prepare_ingest_governance` computes mutable-state volatility and any
   supersession target (`core/service.py:614`).
6. Dedup: by idempotency key, then by `sha256(text|scope|tenant)` content hash; a match
   revives an archived claim and returns early (`core/service.py:624-658`).
7. `evaluate_intake` (P3 admission control) — rejection raises `IntakeRejected` (a
   `ValueError`) and records a `policy_decision` event (`core/service.py:664-686`).
8. Best-effort entity resolution/extraction into the entity registry (`core/service.py:710-751`).
9. `store.create_claim(...)` → `apply_post_ingest_governance` → optional Qdrant sync →
   `claim_ingested` webhook → counters (`core/service.py:752-806`).

### Recall path (hook — the hot path)

1. `recall()` opens a **read-only** `MemoryService` (`recall/context_hook.py:1355`); access
   signals are spooled, never written inline.
2. Salient tokens extracted via `extract_query_tokens`; one FTS5 query per token, unioned
   (OR-semantics workaround for FTS5 AND-joining) — `recall/context_hook.py:1366-1420`.
3. Optional streams layer on: entity fanout, vector fallback, graph traversal, verbatim.
4. `_auto_gate_decide` picks the fusion mode (`recall/context_hook.py:1975`); in `rrf` mode
   each populated stream contributes a ranking to `rrf_fuse` (`recall/context_hook.py:1983-2027`,
   `RRF_K_DEFAULT = 60` at `recall/recall_fusion.py:35`).
5. Budgeted markdown rendering; latency emitted per phase (`recall/context_hook.py:1067`).

### Recall path (service API)

`svc.query_rows()` (`core/service.py:1381`) → RBAC `require_permission` → status/scope
resolution → `legacy` mode delegates to FTS (`_query_legacy_mode:1319`), otherwise the
hybrid path reads a candidate pool (`limit*6`, min 60), filters by temporal currency,
sensitivity and per-agent visibility, ranks with `rank_claim_rows`, optionally LLM-reranks,
and writes a generation-tagged `query_cache` entry (`core/service.py:1481-1570`).

### Steward cycle

`svc.run_cycle()` (`core/service.py:845`) runs inside `llm_budget.cycle_scope()`:
policy selection → extractor → candidate dedupe → deterministic probes → validator → decay
(+ optional Hebbian edge decay) → compactor (opt-in) → rule mining (default OFF) → skill
review. After the budget scope: Qdrant post-cycle sync, integrity, Qdrant reconcile, spool
drain, `recompute_tiers`, integrity metrics. Triggered by CLI `run-cycle`, the MCP
`run_cycle` tool, and the `memorymaster-steward-cycle.py` hook template (`:41`).

**State Management:** all durable state is in the claims DB. Per-cycle LLM budget state is a
`ContextVar` (`core/llm_budget.py:100`); counters are lock-guarded module state
(`core/observability.py:6`).

## Key Abstractions

**Claim** — `core/models.py`; statuses in `CLAIM_STATUSES`, bitemporal `event_time` /
`valid_from` / `valid_until`, tier + scope + visibility + tenant.

**Store protocol (implicit)** — no ABC. Callers probe with `hasattr(self.store, ...)`
(e.g. `core/service.py:629`, `:1497`), and `PostgresStore` inherits from `SQLiteStore`
to get parity by default and overrides dialect-specific methods.

**RetrievalPlan / RetrievalRequest / RetrievalResult** — `recall/planner.py:26-60`.

**Job module** — every steward phase is a module exposing `run(store, ...) -> dict`
(`govern/jobs/*.py`), which is what makes `run_cycle` a flat, failure-isolated pipeline.

## Entry Points

| Entry point | Location | Triggered by |
|---|---|---|
| `memorymaster` CLI | `surfaces/cli.py:main` (also `memorymaster/__main__.py`) | operator, hooks, cron |
| `memorymaster-mcp` | `surfaces/mcp_server.py:main` (`:2735`) | MCP clients over stdio |
| `memorymaster-mcp-http` | `surfaces/mcp_http.py:main` | authenticated streamable HTTP |
| `memorymaster-dashboard` | `surfaces/dashboard.py:main` | operator browser |
| `memorymaster-steward` | `govern/llm_steward.py:main` | scheduled steward |
| `memorymaster-setup` | `surfaces/setup_hooks.py:main` | installer |
| `memorymaster-session-end` | `surfaces/session_end_ingest.py:main` | session-end hook |
| `memorymaster-ops` | `surfaces/operations.py:main` | read-only ops inspection |
| Recall hook | `recall/context_hook.py:recall` via `config_templates/hooks/memorymaster-recall.py:29` | UserPromptSubmit |

(Console scripts declared in `pyproject.toml [project.scripts]`.)

## Architectural Constraints

- **Threading:** mostly single-threaded; locks exist only around shared caches/counters
  (`core/observability.py`, `core/intake_policy.py:155`, `core/key_rotator.py:232`) and in
  `llm_provider`, `llm_rerank`, `qdrant_outbox`, `steward_classifier`, `mcp_server`.
  Per-cycle budget uses `contextvars`, not thread-locals.
- **WAL discipline:** the recall hook must construct the service with `read_only=True`
  (`core/service.py:365-372`) so a prompt can never take a write lock; deferred writes go
  through the spool and are replayed by `govern/jobs/spool_drain.py`.
- **Layer boundary (enforced):** `core|govern|recall|stores` may not import `bridges` or
  `knowledge.wiki`/`knowledge.vault` — `tests/test_extension_boundaries.py`.
- **Size budgets (enforced):** see `tests/test_architecture_budgets.py`; `core/service.py`
  and `surfaces/dashboard.py` currently sit **exactly at** their caps (2450 / 1550 lines).
- **Shim retirement:** root shims may not be removed before 2026-09-30 and only in v5.0+
  (`docs/compatibility.md`, pinned by `tests/test_architecture_budgets.py:64`).
- **Schema:** `schema.sql` / `schema_postgres.sql` create 11 / 13 tables; the rest of the
  schema is applied by ~19 idempotent `_ensure_*` passes in `stores/_storage_schema.py` plus
  20 versioned migrations under `stores/migrations/` run by `migrations/runner.py`.

## Anti-Patterns

### Silent `except Exception: pass` around ingest side-effects

**What happens:** entity resolution, the `entity_id` back-fill and the webhook fire are each
wrapped in a bare swallow (`core/service.py:750`, `:783`, `:804`).
**Why it's wrong:** a genuine store/registry failure is indistinguishable from "feature off",
so entity enrichment can silently stop working repo-wide.
**Do this instead:** follow the `run_cycle` phase pattern — catch, `logger.warning(...)`, and
record the error into the result dict (`core/service.py:906-908`).

### Raw SQL against `claims` from the service layer

**What happens:** `UPDATE claims SET entity_id = ? WHERE id = ?` executed directly on a
connection from the service (`core/service.py:778-781`).
**Why it's wrong:** bypasses the store mixins, the event ledger, and Postgres parity — the
same write on `PostgresStore` is untested by construction.
**Do this instead:** add/extend a method on `_storage_write_claims.py` and call it, so both
backends and the audit trail stay consistent.

### Importing a surface from the core layers

**What happens:** `core/service.py:1796` lazily imports `surfaces.session_tracker`.
**Why it's wrong:** it inverts the stated dependency direction (`surfaces/__init__.py:1-7`);
`test_extension_boundaries.py` does not currently cover `surfaces`, so it went unnoticed.
**Do this instead:** inject the tracker from the surface, or move the tracking primitive
into `core/session_scope.py`.

### Adding a new root-level `memorymaster/*.py` module

**Why it's wrong:** the 109 root modules are a frozen deprecation inventory; a new one has
no removal owner. `docs/compatibility.md` forbids it outright.
**Do this instead:** put the module in the owning subpackage and import the canonical path.

## Error Handling

**Strategy:** fail closed on policy/security, fail soft on enrichment.

- Validation and admission errors raise `ValueError` subclasses (`IntakeRejected`,
  `core/intake_policy.py`) so existing `except ValueError` handlers map them to
  `VALIDATION_ERROR` at the surfaces.
- Authorisation raises `PermissionError` (`stores/postgres_store.py:181-189`).
- Concurrency raises `ConcurrentModificationError` (`stores/_storage_shared.py`).
- LLM overspend raises `LLMBudgetExceeded`, caught once in `run_cycle` to abort cleanly with
  an `aborted_reason` in the result (`core/service.py:930-943`).
- Every steward phase is individually try/except-wrapped so one phase can't kill the cycle.

## Cross-Cutting Concerns

**Logging:** stdlib `logging` with module loggers; hook/latency telemetry via
`core/hook_log.py` and `_emit_recall_latency` (`recall/context_hook.py:1067`).
**Metrics:** counters in `core/observability.py`, exported by `surfaces/metrics_exporter.py`.
**Validation:** `core/security.py` (sanitisation + sensitivity findings + persisted-metadata
validation), `core/models.py:validate_temporal_fields`, `core/intake_policy.py`.
**Authentication/authorisation:** `core/access_control.py:require_permission` for agent RBAC;
tenant/principal/scope binding enforced in `PostgresStore._require_team_authority`
(`stores/postgres_store.py:181`); MCP tools registered through `AuthorizedFastMCP`, which
refuses registration without authorization metadata (`surfaces/mcp_server.py:834`).

---

*Architecture analysis: 2026-08-17*
