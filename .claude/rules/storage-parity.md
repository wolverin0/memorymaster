---
paths:
  - "memorymaster/storage.py"
  - "memorymaster/_storage_read.py"
  - "memorymaster/_storage_write_claims.py"
  - "memorymaster/_storage_lifecycle.py"
  - "memorymaster/postgres_store.py"
  - "memorymaster/schema.sql"
  - "memorymaster/schema_postgres.sql"
  - "memorymaster/db_merge.py"
  - "tests/test_storage*.py"
  - "tests/test_postgres*.py"
  - "tests/test_postgres_parity.py"
---

# Storage Parity Rules

SQLite and Postgres backends MUST stay in sync. Drift = silent data loss when users switch backends.

## The two schemas

| File | Used by | Notes |
|------|---------|-------|
| `memorymaster/schema.sql` | SQLite (default) | FTS5 + WAL mode — single-file, no server |
| `memorymaster/schema_postgres.sql` | Postgres (optional extra) | Requires `psycopg[binary]>=3.2` |

Any column added to one MUST be added to the other in the same PR. Same for indexes, triggers, and constraints.

## Test parity

`tests/test_storage*.py` runs against SQLite. `tests/test_postgres*.py` runs against Postgres if `POSTGRES_URL` is set. Both must pass before merge. If a test is SQLite-only, add a Postgres counterpart — divergence starts here.

## WAL mode is mandatory

SQLite MUST be opened with `PRAGMA journal_mode = WAL`. This is set in `storage.py` inside the `connect()` method's inner `_open()` function (look for `conn.execute("PRAGMA journal_mode = WAL")`). Do not remove — concurrent readers + writers corrupt without it. Symptom of regression: sporadic `database is locked` errors.

## Storage is split across sibling modules

The storage layer is split into `storage.py` (SQLite adapter, connection, init) + `_storage_read.py` + `_storage_write_claims.py` + `_storage_lifecycle.py`. When editing ANY of these, check the others — they share the row schema and must evolve together. Read ops in `_storage_read.py` must decode what write ops in `_storage_write_claims.py` produce.

## FTS5 is the search index

Full-text search on `claims.text` uses SQLite FTS5. If you rename or drop the `claims_fts` virtual table, `query_memory` silently falls back to LIKE-scan — slow and misses token-boundary matches.

## Bidirectional merge (OpenClaw sync)

`db_merge.py` reconciles claims between MemoryMaster and OpenClaw every 15 minutes. Invariants:
- Merge is idempotent — re-running must not create duplicates. Uses `idempotency_key`.
- Conflict resolution prefers the higher `confidence` (ties: newer `updated_at`).
- Never merge across incompatible schema versions — check `schema_version` in both DBs first.

## Never mutate schema at runtime

All schema changes go through migration files (`schema.sql` + `schema_postgres.sql` rewrites + a version bump). No `ALTER TABLE` in code paths. Existing DBs migrate via `setup-hooks.py`.

## When adding a new column

1. Add to `schema.sql` AND `schema_postgres.sql` in the same commit.
2. Update the corresponding dataclass/TypedDict in `storage.py`.
3. Update `postgres_store.py` read/write paths to match.
4. Add a test in both `test_storage*.py` and `test_postgres*.py` that exercises the new column.
5. Bump schema version and write a migration note in `CHANGELOG.md`.

## When in doubt

Run both test files. If one passes and the other doesn't, your change has parity drift — fix before committing.
