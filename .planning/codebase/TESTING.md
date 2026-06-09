# TESTING.md — Testing Strategy

*Regenerated 2026-06-09 from the current tree (v3.28.0). Supersedes the stale v2.0.0 document (which claimed 43 test files).*

## Framework & Scale
- **pytest>=8.2** + **pytest-cov>=6.0** (`dev` extra)
- Config: `pytest.ini` — `testpaths = tests`, `addopts = -p no:cacheprovider`, `norecursedirs = artifacts .pytest_cache .tmp_pytest`, marker `postgres` registered
- **226 test files** (`tests/test_*.py`, counted 2026-06-09) containing **~2,394 `def test_` functions** (grep count)

## Markers
- `@pytest.mark.postgres` — "tests that require a reachable Postgres DSN" (pytest.ini). Skipped unless `MEMORYMASTER_TEST_POSTGRES_DSN` is set.

## Cross-Backend Parity Gate (v3.20.0-S2)
- `tests/conftest.py` provides `parametrize_backends`: the SAME test body runs against a fresh `MemoryService` on both SQLite and Postgres and must produce identical observable results
- SQLite parametrization always runs (file-based); Postgres parametrization is skipped on machines without `MEMORYMASTER_TEST_POSTGRES_DSN`
- This is the regression gate behind the v3.27 batch-1 Postgres parity fixes

## Key Patterns
- **Temp case isolation**: `conftest.py` roots temp DBs under `.tmp_cases/` with cleanup fixtures
- **Migrations are tested**: `tests/test_migrations.py` covers the `MigrationRunner` (discovery, apply, checksum drift)
- **Qdrant/service sync is tested with fakes**: `tests/test_qdrant_backend.py` and `tests/test_service_coverage.py` (`_make_svc_with_qdrant`, `test_sync_upserts_for_confirmed`, `test_sync_deletes_for_archived`, `test_sync_handles_exception`) — no live Qdrant needed
- **No network in core tests**: Gemini / sentence-transformers / Qdrant are mocked, faked, or skipped
- **Sensitivity filter has dedicated tests** (`tests/test_security_access.py` and sensitivity suites); `.claude/rules/sensitivity-filter.md` mandates a red-bar test for every filter change

## Running Tests
```bash
# Full suite (the canonical project command)
python -m pytest tests/ -q --tb=short

# Skip Postgres-dependent tests (default behavior without a DSN)
python -m pytest tests/ -q -m "not postgres"

# Single module
python -m pytest tests/test_migrations.py -v
```

## CI
- `.github/workflows/ci.yml`: matrix {ubuntu-latest, windows-latest} x {3.10, 3.11, 3.12}; installs `.[dev,mcp,security]`; runs `pytest tests/ -q --tb=short` with `MEMORYMASTER_ALLOW_SENSITIVE_BYPASS: "1"` set for the suite
- Separate `perf` job (needs: test) on Python 3.12

## Coverage Areas (current)
- Claim lifecycle, dedup, conflict resolution, compaction, decay/staleness jobs
- Migration framework (apply + drift detection)
- SQLite/Postgres parity (backend-parametrized)
- MCP server tools, sensitivity filter, redaction, access control
- Recall stack: FTS5/lexical, recall fusion, query cache, verbatim recall, Qdrant fallback
- Steward: classifier, features, proposals, LLM provider routing (incl. claude_cli)
- Wiki engine, vault linter, vault bases; dream bridge; db_merge/OpenClaw sync

## Known Gaps
- No end-to-end multi-process concurrency test that reproduces the 12-writer per-pane MCP load profile that corrupted the production DB on 2026-06-05 (race scripts exist only as ad-hoc `scripts/swap_race*.ps1`)
- Dashboard rendering still asserted programmatically, not visually
