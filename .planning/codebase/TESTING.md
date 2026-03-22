# TESTING.md — Testing Strategy

## Framework
- **pytest>=8.2** (`dev` extra)
- Config: `pytest.ini` — `testpaths = tests`, `-p no:cacheprovider`
- 43 test files covering all major modules

## Test Organization
```
tests/
├── conftest.py                    # autouse cleanup fixture for .tmp_cases/
├── test_auto_validate.py
├── test_claim_links.py
├── test_cli_*.py                  # CLI integration tests
├── test_compact_*.py              # Compaction & summary compaction
├── test_config.py
├── test_conflict_resolver.py
├── test_confusion_matrix_eval.py
├── test_connection_retry.py
├── test_connectors.py
├── test_context_optimizer.py
├── test_conversation_to_turns.py
├── test_dashboard.py
├── test_dedup.py
├── test_deterministic_predicates.py
└── ... (43 total)
```

## Key Patterns
- **In-memory SQLite**: tests use `SQLiteStore(":memory:")` or temp files in `.tmp_cases/`
- **Temp case cleanup**: `conftest.py` autouse fixture prunes `.tmp_cases/` before and after each test
- **Postgres skip marker**: `@pytest.mark.postgres` — skipped unless a real Postgres DSN is reachable
- **No network in core tests**: Qdrant, Gemini, sentence-transformers mocked or skipped

## Running Tests
```bash
# All tests (requires dev extras installed)
pytest

# Skip slow/integration tests
pytest -m "not postgres"

# Single module
pytest tests/test_claim_links.py -v
```

## Coverage Areas
- Claim lifecycle (ingest → confirm → decay → archive)
- Deduplication with embedding similarity
- Context optimizer token packing
- CLI JSON output flag
- MCP server tool definitions
- Security/redaction workflows
- Conflict resolution
- Event log append-only enforcement
- Connection retry logic
- Dashboard rendering
