# CONCERNS.md — Risks & Technical Concerns

*Regenerated 2026-06-09 from the current tree (v3.28.0). Supersedes the stale v2.0.0 document. Resolved v2.0-era concerns are listed at the bottom.*

## Current Risks (2026-06)

### 1. Many concurrent per-pane MCP writers on one ~3GB SQLite file — corruption HAS happened
- Each Claude Code pane spawns its own stdio `mcp_server.py` process; with ~12 panes open, that is ~12 independent writer processes (plus steward cron, OpenClaw sync, stop-hook extractors) all writing the same `memorymaster.db`
- **This is not theoretical**: the DB corrupted on 2026-06-05 — recovery artifacts are in the tree (`memorymaster.db.corrupt-2026-06-05`, `scripts/recover_db_indexcorrupt.py`, `scripts/FINISH_DB_SWAP.bat`, `scripts/swap_race*.ps1`)
- WAL + `busy_timeout` (storage.py:42-48; hardened in v3.27 batch 2) prevent `database is locked` errors but do NOT make N independent processes on a 3GB file safe against index corruption under sustained concurrent write load
- No central write daemon / single-writer funnel exists; mitigation options remain open: shared long-lived MCP daemon, write queue, or moving multi-agent deployments to the Postgres backend (which exists and is parity-tested)

### 2. Intake-vs-steward throughput imbalance (candidate backlog)
- Hooks ingest candidates on every prompt/stop across many panes; the steward validates on a 6-hour cron — intake rate structurally exceeds validation rate
- Symptom already hit production: commit 60e40ec ("fix(steward): batch_limit threads into all cycle jobs (drain candidate backlog)") was needed to drain the backlog
- Unvalidated candidates degrade recall precision and grow the DB (compounding concern #1's file size)

### 3. Qdrant sync is fire-and-forget with no reconciliation
- `MemoryService._qdrant_sync()` (service.py:323) silently swallows all exceptions on per-claim upsert/delete; `_qdrant_post_cycle_sync()` (service.py:335) only re-upserts after a steward cycle
- `QdrantBackend.sync_all()` exists but is manual — there is no scheduled reconciliation job, drift metric, or health check comparing Qdrant point count vs SQLite truth
- If Qdrant is flaky or down for a stretch, vector recall silently serves stale/missing results until someone runs a full re-index (`scripts/index_claims_to_qdrant.py`)

### 4. Flat-module sprawl: ~110 top-level modules
- `memorymaster/` has 108 flat `.py` modules (plus `jobs/`, `connectors/`, `migrations/`) — recall, wiki, vault, steward, verbatim, entity, and rules subsystems all share one namespace
- Cost: discoverability, import-cycle risk, and onboarding; the `_storage_*` / `cli_handlers_*` split kept files under 800 LOC but multiplied module count instead of introducing subpackages
- No correctness bug today, but every new feature (v3.28 added more wiki_*/rule_* modules) makes an eventual subpackage refactor more expensive

### 5. Per-pane MCP server processes (operational footprint)
- Beyond the write-concurrency risk (#1): N panes = N Python processes each holding the 3GB DB open, each with its own query cache and Qdrant client; no shared state, no cross-pane rate limiting on LLM-backed tools
- Restarting a pane silently restarts its server; there is no version check, so long-lived panes can run STALE code against a migrated schema (migrations apply on connect, but old code paths remain)

### 6. Carried-over minor concerns (still true)
- LLM API keys accepted via env/args; key-rotation cooldown (`key_rotator.py`, `llm_budget.py`) is best-effort under burst load
- `cryptography` payload encryption: key management is entirely external — a lost key means unrecoverable payloads
- `MEMORYMASTER_ALLOW_SENSITIVE_BYPASS=1` is set in CI (ci.yml); it must never leak into a production environment (default-off is test-asserted in `tests/test_security_access.py`)
- Context packing token counts are still estimates (no real tokenizer)

## FIXED since the v2.0 document

| v2.0-era concern | Status | Evidence |
|---|---|---|
| "No migration framework, no version tracking" (#7) | **FIXED v3.20.0-S1** | `memorymaster/migrations/` with `MigrationRunner`, `schema_versions` checksum bookkeeping, drift detection (`MigrationDriftError`); commit 2067a64; tested in `tests/test_migrations.py` |
| "Risk of schema drift between SQLite and PostgreSQL" (#7) | **FIXED v3.27 batch 1 + v3.20.0-S2 gate** | Parity remediation commit 9a9c3d6 ("postgres parity, merge, wiki, mcp-security"); cross-backend `parametrize_backends` gate in `tests/conftest.py` (commit 28d6cc1); every migration ships `apply_sqlite` + `apply_postgres` |
| "SQLite is single-writer; concurrent MCP calls may cause database-is-locked" (#1, the lock-error half) | **HARDENED v3.27 batch 2** | Commit b1fcc92 ("remediate 24 medium findings — concurrency, visibility, integrity"); `PRAGMA journal_mode=WAL` + `busy_timeout=5000` on every store connection (storage.py), 30000ms on long-lived writers (`db_merge.py:292`, `contradiction_probe.py:86`); "WAL + busy_timeout are mandatory for every SQLite writer" is now a documented codebase invariant. NOTE: lock ERRORS are fixed; multi-process corruption risk is NOT (see #1 above) |
| "Zero-dependency core has hidden coupling" (#2) | **OBSOLETE** | Core is no longer zero-dep (`requests`, `tenacity` are mandatory); optional extras are explicit and runtime-gated (`MEMORYMASTER_RECALL_GRAPH`, `QDRANT_URL`, provider env vars) |
| "Qdrant sync is fire-and-forget" (#5) | **STILL OPEN** | Carried forward as #3 above — only partial mitigation (`_qdrant_post_cycle_sync`, manual `sync_all`) shipped |
