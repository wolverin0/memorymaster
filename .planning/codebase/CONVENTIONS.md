# CONVENTIONS.md — Code Conventions

*Regenerated 2026-06-09 from the current tree (v3.28.0). Supersedes the stale v2.0.0 document.*

## Python Style
- `from __future__ import annotations` in every module; `X | Y` union syntax (3.10+)
- Type hints on all signatures; ruff (E/F/W, line-length 120) + mypy (`check_untyped_defs`) configured in pyproject.toml
- `@dataclass(frozen=True)` / `(slots=True)` for value objects (see `migrations/runner.py:Migration`, `MigrationStatus`)
- `logging.getLogger(__name__)` everywhere; no prints in library code

## Enforced Rules (`.claude/rules/`)
| Rule file | Scope | Key invariants |
|---|---|---|
| `claims-lifecycle.md` | always-on | Six canonical statuses from `models.py:CLAIM_STATUSES`; transitions only via `service.py` / `_storage_lifecycle.py`, never raw SQL; supersession sets BOTH `supersedes_claim_id` and `replaced_by_claim_id`; tiers `core`/`working`/`peripheral` (no schema CHECK — stay canonical); scopes `project:<slug>` / `user` / `team:<name>` / `global`; bitemporal `event_time`/`valid_from`/`valid_until` in ISO-8601 |
| `sensitivity-filter.md` | always-on | Filter runs on EVERY ingest path (mcp_server, dream_bridge, service.ingest); never add an `allow_sensitive` bypass; every filter change ships a red-bar test |
| `storage-parity.md` | storage.py, postgres_store.py, schema*.sql, db_merge.py | SQLite + Postgres stay in sync; WAL, FTS5 |
| `mcp-server.md` | mcp_server.py | Every tool gets auto-citation fallback (CitationInput), passes the sensitivity filter, and sets `source_agent` on `svc.ingest()` |
| `python/*.md` (ECC upstream) | `**/*.{py,pyi}` | PEP 8, type annotations, frozen dataclasses, pytest, bandit |

## Module Organization
- **Flat package, files under 800 LOC**: oversized modules are split with underscore prefixes — `storage.py` fronts `_storage_{schema,read,write_claims,lifecycle,sources,shared}.py`; `cli.py` fronts `cli_handlers_basic.py` / `cli_handlers_curation.py` / `cli_helpers.py`
- `models.py` — pure data + validation, no I/O; `service.py` — orchestrator; stores do DB I/O only
- `jobs/` — each job exposes `run(...)` returning a dict of counts; jobs never raise on partial failure
- `connectors/` — external channel adapters (e.g. `whatsapp.py`)
- `mcp_server.py` — thin adapter over `MemoryService` (30 `@mcp.tool` definitions)

## Migrations Discipline
- New schema changes go in `memorymaster/migrations/NNNN_short_description.py` defining `VERSION`, `DESCRIPTION`, `apply_sqlite(conn)`, `apply_postgres(conn)` — both backends, always
- **Migrations are immutable once applied**: `MigrationRunner` stores a sha256 checksum in `schema_versions` and raises `MigrationDriftError` if the file changes afterward — write a new migration instead
- Version numbers may skip (0005 was never shipped); duplicates are a hard error in `discover_migrations`
- Schema changes also require updating `schema.sql` + `schema_postgres.sql` + `storage.py` + `postgres_store.py` + tests (AGENTS.md boundary)

## Database Patterns
- **WAL + busy_timeout are mandatory for every SQLite writer** (stated explicitly in `contradiction_probe.py:78` and `db_merge.py:283`): `storage.py` sets `journal_mode=WAL` + `busy_timeout=5000`; long-lived writers like `db_merge.py` and `contradiction_probe.py` use `busy_timeout=30000`
- Append-only event log enforced by SQLite triggers
- `idempotency_key` for safe re-ingestion
- Backend-aware access goes through `store_factory.py` — do not hand-open connections in new code (see `cli_handlers_curation.py:202`)

## Naming & Configuration
- Env vars: `MEMORYMASTER_*` prefix throughout (`config.py` documents the retrieval-weight family; `MEMORYMASTER_LLM_PROVIDER`, `MEMORYMASTER_RECALL_GRAPH`, `MEMORYMASTER_CLAUDE_CLI_BIN`, `MEMORYMASTER_TEST_POSTGRES_DSN`, `QDRANT_URL` is the one legacy exception)
- Optional config file via `MEMORYMASTER_CONFIG_FILE` (JSON overrides)
- Stores: `XxxStore`; service methods: snake_case verbs (`ingest`, `query`, `run_cycle`)
- Conventional commits (feat:/fix:/test:/chore:), atomic

## Error Handling
- `ValueError` for invalid input, `RuntimeError` for impossible state
- External integrations (Qdrant, embeddings, claude_cli subprocess) degrade gracefully with `logger.warning` — never crash the ingest/query path
- Retries via `tenacity` (now a mandatory dep) and `retry.py`

## Security Conventions
- Sensitive detection in `security.py` (`is_sensitive_claim`), sanitization on every ingest, display-time redaction is a SEPARATE layer from the ingest filter
- `MEMORYMASTER_ALLOW_SENSITIVE_BYPASS` exists for the CI test suite only (`is_sensitive_bypass_enabled`, default disabled per `tests/test_security_access.py`)
- No hardcoded IPs/paths/credentials — env vars only
