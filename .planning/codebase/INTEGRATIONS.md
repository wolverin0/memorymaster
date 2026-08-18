# External Integrations

**Analysis Date:** 2026-08-17
**Version analysed:** MemoryMaster 4.7.6, commit `c832df4`

MemoryMaster is local-first: the only mandatory external surface is the filesystem. Every network integration below is optional and off unless configured.

## Outward Surfaces

### 1. MCP server — `memorymaster/surfaces/mcp_server.py` (2743 lines)

FastMCP stdio server (`memorymaster-mcp`). **50 registered `@mcp.tool()` functions**:

| Group | Tools |
|-------|-------|
| Lifecycle / setup | `init_db`, `resolve_project`, `open_dashboard` |
| Simple memory verbs | `remember`, `recall`, `forget`, `forget_preview`, `improve` |
| Session scope | `session_scope_show`, `session_scope_bind`, `session_scope_clear` |
| Ingest | `ingest_claim`, `checkpoint`, `archive_by_source` |
| Recall / query | `query_memory`, `query_for_context`, `query_for_task`, `query_claim_paths`, `query_meta_decisions`, `recall_analysis`, `classify_query`, `volunteer_context`, `read_active_tasks`, `list_claims`, `search_verbatim`, `find_related_claims` |
| Rules | `ingest_rule`, `query_rules`, `rules_export` |
| Governed skills | `skill_inputs`, `skill_propose`, `skill_review`, `skill_recall`, `skill_export` |
| Steward / governance | `run_cycle`, `run_steward`, `list_steward_proposals`, `resolve_steward_proposal`, `compact_memory`, `recompute_tiers`, `quality_scores` |
| Privacy | `redact_claim_payload`, `pin_claim` |
| Entities | `extract_entities`, `entity_stats` |
| Telemetry | `list_events`, `get_usage_rollup`, `dream_status` |
| Cross-repo | `federated_query`, `local_search` |

Cross-cutting behaviour every tool inherits:
- **Input validation** → structured errors, never raw tracebacks (`_structured_error:126`, `_validate_tool_input:140`). Codes in use: `VALIDATION_ERROR`, `INVALID_INPUT`, `MISSING_FIELD`, `INPUT_TOO_LONG`, `SENSITIVE_INPUT`, `RATE_LIMITED`, `ENTITY_GRAPH_NOT_READY`.
- **Sensitivity scan on input** including decoded base64/hex-escape variants (`_sensitivity_scan_variants:183`, `_sensitive_input_error:193`).
- **Rate limiting** — token bucket per key, `MM_INGEST_RATE_LIMIT_PER_MIN` (`_check_ingest_rate_limit:243`), plus a durable ingest quota (`_check_durable_ingest_quota:296`).
- **Path policy** — DB/workspace resolution is allowlisted (`memorymaster/surfaces/mcp_path_policy.py`, `MEMORYMASTER_MCP_DB_ALLOWLIST`), read-only services for query tools (`_read_service:399`).
- **Authorization** — `_authorized_tool_callable:805` wraps tools with `McpToolPolicy`; `MEMORYMASTER_MCP_AUTH_MODE` (`local-trusted` is the named local profile).
- **Usage telemetry** — `_record_mcp_usage:427`, `_usage_rollup:403`.

### 2. MCP over HTTP — `memorymaster/surfaces/mcp_http.py`

Streamable-HTTP entrypoint (`memorymaster-mcp-http`) on Starlette. Bearer-token auth via `BearerAuthMiddleware` (`mcp_http.py:23`), token from `MEMORYMASTER_MCP_HTTP_TOKEN`, host allowlist `MEMORYMASTER_MCP_HTTP_ALLOWED_HOSTS` (default `127.0.0.1:*`, `localhost:*`, `[::1]:*`). `/healthz` and `/readyz` stay unauthenticated for probes. **Stdio remains the default transport.**

### 3. CLI — `memorymaster/surfaces/cli.py`

116 `add_parser` subcommands in `cli.py` plus 5 more in `cli_handlers_skills.py` (`skill-inputs`, `skill-propose`, `skill-review`, `skill-recall`, `skill-export`). Handlers split across `cli_handlers_basic.py`, `cli_handlers_curation.py`, `cli_handlers_integrity.py`, `cli_handlers_public.py`, `cli_handlers_skills.py`.

Families: db (`init-db`, `migrate`, `snapshot`, `snapshots`, `rollback`, `diff`, `repair-fk`, `integrity`) · ingest (`ingest`, `remember`, `ingest-daydream`, `import-whatsapp`, `extract-claims`, `extract-atlas-claims`) · recall (`query`, `recall`, `context`, `query-paths`, `federated-query`) · governance (`run-cycle`, `run-steward`, `steward-proposals`, `resolve-proposal`, `resolve-conflicts`, `dedup`, `decay`, `compact`, `compact-summaries`, `recompute-tiers`, `verify-claims`, `detect-contradictions`, `review-queue`) · wiki (`wiki-absorb`, `wiki-cleanup`, `wiki-breakdown`, `wiki-suggest-links`, `lint-vault`, `export-vault`, `curate-vault`, `bases-generate`) · dream (`dream-seed`, `dream-ingest`, `dream-sync`, `dream-clean`, `dream-run`, `dream-status`) · entities (`extract-entities`, `entity-list`, `entity-merge`, `entity-aliases`, `entity-backfill`, `entity-graph-export`) · actions/media (`propose-actions`, `action-proposals`, `resolve-action-proposal`, `export-actions`, `transcribe-source-item`, `ocr-source-item`, media-retry queue commands) · sync (`merge-db`, `qdrant-sync`, `qdrant-reconcile`, `drain-spool`) · servers (`run-daemon`, `run-dashboard`, `run-operator`) · setup (`install-hook`, `install-gitnexus-hook`).

### 4. Dashboard — `memorymaster/surfaces/dashboard.py`

stdlib `http.server` (`memorymaster-dashboard`, port 8765). Token auth via `dashboard_auth.py`: `MEMORYMASTER_DASHBOARD_TOKEN_VIEWER` / `MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR`. Binding off-loopback requires the explicit `MEMORYMASTER_DASHBOARD_UNSAFE_BIND` escape hatch. Read models and mutations are split into `dashboard_read_models.py` / `dashboard_commands.py` (enforced by `tests/test_architecture_budgets.py:52`).

### 5. Prometheus metrics — `memorymaster/surfaces/metrics_exporter.py`

In-process counters/histograms in `memorymaster/core/observability.py` (buckets 0.005s → 10s), rendered as Prometheus text. CLI: `export-metrics`.

## Hooks installed into agent clients

`memorymaster/surfaces/setup_hooks.py` (`memorymaster-setup`) writes `~/.claude/settings.json` and copies templates from `memorymaster/config_templates/hooks/`:

| Client event | Hook script | Timeout | Default |
|---|---|---|---|
| `UserPromptSubmit` | `memorymaster-recall.py` then `memorymaster-classify.py` | 5s | on |
| `SessionStart` (`startup\|resume`) | `memorymaster-session-start.py` | 10s | on |
| `SessionEnd` | `memorymaster-session-end.py` | 30s | on |
| `Stop` | `memorymaster-auto-ingest.py` | 10s | on |
| `PreCompact` | `memorymaster-precompact.py` | 15s | on |
| `PostToolUse` (`Edit\|Write`) | `memorymaster-validate-wiki.py` | 5s | on |
| `PreToolUse` (`Grep\|Glob`) | `memorymaster-pretooluse-recall.py` | 5s | **opt-in** — needs `--pretooluse` AND `MEMORYMASTER_PRETOOLUSE_RECALL=1` (`setup_hooks.py:417-429`) |

Dream hooks (`memorymaster-dream-capture.py`, `memorymaster-dream-sync.py`) install separately via `install_dream_hooks(install_claude=…, install_codex=…)` (`setup_hooks.py:450`).

Setup profiles (`SETUP_PROFILES`, `setup_hooks.py:57`): `minimal` (db+mcp) · `semantic` (+provider, vector_backend) · `team` (db+mcp) · `full-lab` (all, incl. recall/capture hooks, steward, dashboard). `evaluate_setup_profile` returns PASS / PARTIAL(3) / BLOCKED(2).

**MCP client registration:** Claude Code via `~/.claude.json` `mcpServers` (`setup_hooks.py:861-873`); Codex via `~/.codex/config.toml` (`install_mcp_codex:884`) plus `~/.codex/AGENTS.md` append and `~/.codex/hooks.json`.

## Bridges — `memorymaster/bridges/`

| Bridge | File | Status |
|---|---|---|
| **Delta sync** | `delta_sync.py` | Active. `export_delta` writes a small SQLite file with only claims+citations changed since a watermark; consumed unchanged by `merge-db`. Replaces whole-DB copies (2.5 GB → KB). |
| **DB merge** | `db_merge.py` | Active. Bidirectional append-only merge; dedups on `idempotency_key` + text hash; redacts content and rejects secret-shaped metadata before target writes. Accepts local paths and `user@host:/path`. |
| **Local search** | `local_search/` (`everything.py`, `provider.py`, `redact.py`, `resolver.py`) | Active, Windows-only backend. Wraps Everything's `ES.exe` (`MEMORYMASTER_EVERYTHING_ES_PATH`). Read-only, never ingests; output paths collapsed to root-relative tokens so `C:\Users\<name>` never reaches a transcript; returns `degraded: true` when the backend is missing. `LocalSearchProvider` Protocol leaves room for plocate/fd/mdfind. |
| **Atlas — deterministic** | `atlas_claim_extractor.py` | Active. Keyword/regex extraction from source evidence. |
| **Atlas — LLM** | `atlas_llm_extractor.py` | Active, CLI-selected alternative. Reads evidence bodies + sender + date + provider, emits 0..N typed claims or nothing for newsletters/bots/OTP/receipts. Malformed LLM output → skip + `degraded`, never a junk claim. Provider-aware citations (`gmail://`, `outlook://`, …). |
| **Atlas contract** | `atlas_contract.py`, `evidence_policy.py`, `persisted_envelope.py` | Active. `evidence_policy.is_synthetic_provider` permanently excludes mock/synthetic/placeholder/fake/fixture-derived evidence from governed knowledge and action paths. |
| **Dream bridge** | `dream_bridge.py` | Active. Export/import to Claude Code Auto Dream memory (`~/.claude/projects/<slug>/memory/`). Applies an **extra** export-time filter (`_DREAM_EXTRA_PATTERNS`) that redacts private IPs — deliberately not filtered at ingest. |
| **Media** | `media_processing.py`, `media_providers.py` | Active, opt-in. `TranscriptionProvider` / `OcrProvider` Protocols; OpenAI Whisper adapter (stdlib urllib, `OPENAI_API_KEY` + `OPENAI_BASE_URL`) and Tesseract OCR (needs `pytesseract` + system binary). Adapters raise `RuntimeError`; `_process_media` records a `media_process` event with `media_process_failed` and preserves the source item. |
| **Connectors** | `connectors/whatsapp.py` | Active. CLI `import-whatsapp`. |
| **QMD bridge** | `qmd_bridge.py` | **KEEP-DEPRECATED** (operator verdict 2026-06-10). Zero package importers, test-only surface. |
| **Federated graphify** | `federated_graphify.py` | **KEEP-DEPRECATED** (2026-06-10). Superseded by `service.federated_query`, which is what the live MCP/CLI paths call. |

## Data Storage

- **Primary:** SQLite single file, WAL. Default path from `MEMORYMASTER_DEFAULT_DB`.
- **Optional:** Postgres via `MEMORYMASTER_POSTGRES_DSN` / `DATABASE_URL` / `POSTGRES_DSN` (`psycopg`), with tenant RLS.
- **Vector:** Qdrant at `QDRANT_URL` (+ `QDRANT_API_KEY`, `QDRANT_CA_CERT`). **Reads quarantined** — `qdrant_backend.py:443` and `qdrant_recall_fallback.py:243` raise `PermissionError`; only index sync runs.
- **Graph:** Kuzu embedded file, opt-in via `MEMORYMASTER_RECALL_GRAPH=1`.
- **Spool:** durable write envelopes under `MEMORYMASTER_SPOOL_DIR`, drained by `drain-spool` / the steward.
- **Snapshots:** `VACUUM INTO` under `MEMORYMASTER_SNAPSHOT_DIR` (default `~/.memorymaster/snapshots/`, keep-3 rotation).
- **Wiki export:** Obsidian vault, **off by default** — requires `MEMORYMASTER_WIKI_ABSORB=1` (`mcp_server.py:81`).

## Authentication & Identity

- MCP stdio: `MEMORYMASTER_MCP_AUTH_MODE` + path allowlist. MCP HTTP: bearer token + host allowlist.
- Dashboard: viewer/operator token split.
- Postgres: tenant + principal identity carried into RLS policies (`stores/postgres_policy_contract.py`, migrations 0008–0012).
- LLM providers: per-provider API-key env vars with round-robin rotation and cooldown (`core/key_rotator.py`).

## Monitoring & Observability

- **Metrics:** in-process counters/histograms (`core/observability.py`) → Prometheus text (`surfaces/metrics_exporter.py`).
- **Health:** `/healthz`, `/readyz` on dashboard and MCP-HTTP; CLI `ready`.
- **Logs:** stdlib `logging` per module (`logger = logging.getLogger(__name__)`).
- **Events ledger:** every lifecycle transition writes a typed row (`EVENT_TYPES`, `core/models.py:18`); CLI `list-events`, `history`.
- **Hook telemetry:** `core/hook_log.py`; MCP usage rollups via `get_usage_rollup`.

## Webhooks & Callbacks

**Outgoing** — `memorymaster/core/webhook.py`. Fires on claim events when `MEMORYMASTER_WEBHOOK_URL` is set. HMAC-SHA-256 signing activates only when `MEMORYMASTER_WEBHOOK_SECRET` is set; headers `X-MemoryMaster-Signature: sha256=<hex>` and `X-MemoryMaster-Timestamp`, signing input `{timestamp}.{body}`, with a replay window.

**Incoming** — none. No inbound HTTP endpoint accepts external writes; the dashboard and MCP-HTTP are the only listeners and both are token-gated and loopback-bound by default.

## Deployment

- **Container:** root `Dockerfile`; `docker-compose.yml` runs the dashboard (or any entrypoint via `MEMORYMASTER_SERVICE_ENTRYPOINT`) plus Qdrant (TLS + API key, image pinned by digest) and Ollama. All host ports bound to `127.0.0.1`. CPU/memory limits set per service.
- **Kubernetes:** `helm/memorymaster/`, two service profiles (`dashboard`, `mcp-http`). The chart requires `image.digest` — CI renders and lints both profiles.

---

*Integration audit: 2026-08-17*
