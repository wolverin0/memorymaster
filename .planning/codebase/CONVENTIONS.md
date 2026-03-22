# CONVENTIONS.md — Code Conventions

## Python Style
- `from __future__ import annotations` in every module (PEP 563 deferred evaluation)
- Type hints throughout; uses `X | Y` union syntax (Python 3.10+)
- `@dataclass(slots=True)` for model classes (performance + immutability signal)
- `collections.abc` imports for abstract types (e.g., `Mapping`, `Sequence`)

## Module Organization
- **models.py** — pure data + validation logic, no I/O
- **service.py** — business logic orchestrator, thin wrapper over store + jobs
- **storage.py / postgres_store.py** — DB I/O only; no business logic
- **jobs/** — each job is a standalone `run()` function returning a dict result
- **mcp_server.py** — thin adapter; delegates to `MemoryService`

## Naming
- Stores: `SQLiteStore`, `PostgreSQLStore` — consistent `XxxStore` pattern
- Jobs: `extractor.run()`, `validator.run()`, etc. — all `run(store, ...)` signature
- Service methods: snake_case verbs (`ingest`, `query`, `run_cycle`, `compact`)
- Environment variables: `MEMORYMASTER_*` prefix

## Error Handling
- `ValueError` for invalid inputs (claim_id, empty text, missing citations)
- `RuntimeError` for unexpected state (claim disappeared after write)
- Job functions return dicts with counts, never raise on partial failures
- External integrations (Qdrant, embeddings) fail silently with `logger.warning`

## Logging
- `logging.getLogger(__name__)` in every module
- Debug-level for routine ops, warning for recoverable errors, no prints

## Database Patterns
- SQLite: WAL mode, `PRAGMA foreign_keys = ON`, `row_factory = sqlite3.Row`
- Append-only event log enforced by DB triggers
- `idempotency_key` with `get_claim_by_idempotency_key` for safe re-ingestion
- Schema migrations handled by `_ensure_*_schema()` methods in `SQLiteStore`

## Security
- Sensitive claims detected in `security.py` via `is_sensitive_claim()`
- PII/secrets redacted or encrypted before storage
- `sanitize_claim_input()` applied on every ingest
- `resolve_allow_sensitive_access()` guards all query paths

## Testing
- pytest with fixtures in `conftest.py`
- In-memory SQLite (`:memory:`) for unit tests
- Test files: `test_<module_or_feature>.py`
