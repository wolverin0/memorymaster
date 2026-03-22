# CONCERNS.md — Risks & Technical Concerns

## 1. SQLite Concurrency
- SQLite is single-writer; concurrent MCP server calls or parallel agents may cause `OperationalError: database is locked`
- `retry.py` implements `connect_with_retry()` but retry strategy should be verified for production multi-agent use
- **Mitigation**: use PostgreSQL backend for production multi-agent deployments

## 2. Zero-Dependency Core Has Hidden Coupling
- `service.py` imports from `embeddings.py`, `security.py`, `retrieval.py` etc.
- If optional packages (sentence-transformers, cryptography) are installed, they activate silently
- Failure modes if partially installed: `create_best_provider()` may fall back silently
- **Risk**: unexpected behavior when moving between environments

## 3. Event Log Immutability Via Triggers
- Append-only enforced by SQLite triggers (`trg_events_append_only_*`)
- If schema migrations fail silently, triggers may not be installed → events can be mutated
- `_ensure_event_integrity_schema()` called on every `init_db()` but not validated at runtime

## 4. LLM Steward API Keys
- `compact_summaries.py` / `llm_steward.py` accept API keys as function args or env vars
- Keys logged via `logger.warning` paths if connection fails (partial leak risk)
- Multi-key rotation (`api_keys` list) with `cooldown_seconds` — may not handle rate limits gracefully under burst load

## 5. Qdrant Sync Is Fire-and-Forget
- `_qdrant_sync()` silently swallows all exceptions
- Vector index may diverge from SQLite truth if Qdrant is flaky
- No reconciliation job or health check for vector-DB consistency

## 6. Sensitive Claim Encryption
- `security.py` uses `cryptography>=42` for payload encryption
- Key management not defined in codebase — keys must come from environment
- If key is lost, encrypted payloads are permanently irrecoverable

## 7. Schema Migrations
- `_ensure_*_schema()` methods in `SQLiteStore` are additive only (ALTER TABLE / CREATE IF NOT EXISTS)
- No migration framework, no version tracking
- Risk of schema drift between SQLite and PostgreSQL if they diverge

## 8. Dedup Threshold Sensitivity
- Default dedup threshold `0.92` + `min_text_overlap 0.3`; false-positive dedup possible
- Without semantic embeddings (sentence-transformers not installed), similarity is keyword-based only — may miss near-duplicates

## 9. Context Optimizer Token Budget
- `pack_context()` uses a greedy knapsack — not optimal
- Token count estimation is approximate (no actual tokenizer)
- May over/under-fill context window for models with non-standard tokenization

## 10. Test Coverage Gaps
- `test_confusion_matrix_eval.py` suggests eval metrics exist but may not be regression-tested
- Dashboard TUI (`dashboard.py`) tested but UI rendering is hard to assert programmatically
- No integration test for the full MCP server → agent round-trip
