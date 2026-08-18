# Technology Stack

**Analysis Date:** 2026-08-17
**Version analysed:** MemoryMaster 4.7.6 (`pyproject.toml:7`), commit `c832df4`

## Languages

**Primary:**
- Python 3.10+ — the entire `memorymaster/` package (~305 modules). `requires-python = ">=3.10"` (`pyproject.toml:24`); classifiers cover 3.10 / 3.11 / 3.12.
- SQL — hand-written schema in `memorymaster/schema.sql` (278 lines, 11 `CREATE TABLE`) and `memorymaster/schema_postgres.sql` (415 lines).

**Secondary:**
- Bash / PowerShell — operator scripts under `scripts/` (`hermes-sync.sh`, `FINISH_DB_SWAP.bat`).
- YAML — CI (`.github/workflows/`), Compose, Helm chart (`helm/memorymaster/`).

## Runtime

**Environment:**
- CPython 3.10–3.12. No async framework in the core; the MCP HTTP surface runs on Starlette/ASGI (`memorymaster/surfaces/mcp_http.py:11`).
- Windows is a first-class dev target — `setup_hooks.py:50` branches on `platform.system() == "Windows"`, and `pytest.ini` documents Windows-specific run rules.

**Package Manager:**
- pip + setuptools (`build-backend = "setuptools.build_meta"`, `pyproject.toml:3`).
- No lockfile. Dependency floors only (`>=`), no upper pins except `mcp>=1.8.1,<2`.
- `packages.find` is scoped to `include = ["memorymaster*"]` (`pyproject.toml:79`) so `tests/`, `scripts/`, `benchmarks/` are deliberately NOT shipped in the wheel. Root `conftest.py` re-adds the repo root to `sys.path` so tests can still `from scripts import ...`.

## Frameworks

**Core (runtime dependencies — only two):**
- `requests>=2.31` — HTTP for LLM providers and webhooks.
- `tenacity>=8.2` — retry decorators.

Everything else is an optional extra. A bare `pip install memorymaster` gives SQLite storage + CLI with no vector search, no MCP, no Postgres.

**Testing:**
- `pytest>=8.2`, `pytest-cov>=6.0`, `pytest-timeout>=2.3` (extra `dev`). See TESTING.md.

**Build/Dev:**
- `ruff` — lint + format, `target-version = "py310"`, `line-length = 120`, rules `E,F,W`, `E501` ignored (`pyproject.toml:64-70`).
- `mypy` — configured (`python_version = "3.10"`, `check_untyped_defs = true`, `ignore_missing_imports = true`, `pyproject.toml:89-92`) but **not run in CI** — `.github/workflows/ci.yml` has no mypy step.
- `pip-audit>=2.10` in the `dev` extra.

## Key Dependencies (optional extras)

| Extra | Packages | Enables |
|-------|----------|---------|
| `postgres` | `psycopg[binary]>=3.2` | `memorymaster/stores/postgres_store.py`, tenant RLS |
| `mcp` | `mcp>=1.8.1,<2` | FastMCP stdio + streamable-HTTP server |
| `security` | `cryptography>=42` | key rotation / at-rest crypto helpers |
| `embeddings` | `sentence-transformers>=3.0` | local 384-dim embeddings |
| `vector` | `sentence-transformers>=3.0`, `qdrant-client>=1.9` | Qdrant recall fallback + `scripts/index_claims_to_qdrant.py` |
| `qdrant` | `httpx>=0.27` | Qdrant HTTP transport |
| `graph` | `kuzu>=0.4` | embedded graph retrieval stream (`memorymaster/recall/graph_store.py`), gated by `MEMORYMASTER_RECALL_GRAPH=1` |
| `gemini` | `google-genai>=1.0` | Google provider SDK path |
| `capture` | `pypdf>=5.0`, `python-docx>=1.1` | document capture adapters |
| `ml` | `scikit-learn>=1.3`, `joblib>=1.3` | steward classifier / RL trainer |

The MCP import is soft: `mcp_server.py:40-42` does `from mcp.server.fastmcp import FastMCP` inside a `try` and sets `FastMCP = None` on failure, so the package imports without the extra.

## Storage Stack

**SQLite (default, single file):**
- Connection discipline is centralised in `memorymaster/stores/_storage_shared.py:128-130`: `PRAGMA foreign_keys = ON`, `PRAGMA journal_mode = WAL`, `PRAGMA busy_timeout` (default **15000 ms**, replacing divergent 0/5000/30000 ms ad-hoc values). A shorter `query_ms` timeout variant exists for read paths (`_storage_shared.py:154`).
- FTS5 is **probed, not assumed**: `_storage_schema.py:406-428` creates a throwaway `_fts5_probe` virtual table and only builds `claims_fts` when FTS5 is compiled in.
- Schema is split: base DDL in `memorymaster/schema.sql`, incremental migrations in `memorymaster/stores/migrations/0001_initial.py` … `0021_compiled_user_profile.py` driven by `migrations/runner.py`.

**Postgres (optional, multi-tenant):**
- `memorymaster/stores/postgres_store.py` behind `memorymaster/stores/store_factory.py`.
- Row-level security migrations: `0008_postgres_tenant_rls.py`, `0011_postgres_scoped_force_rls.py`. Policy contract asserted by `memorymaster/stores/postgres_policy_contract.py`.
- Local dev server: `docker-compose.postgres.yml` (postgres:16-alpine pinned by digest, host port 6543).

**Qdrant (vector, currently quarantined):**
- Transport `memorymaster/recall/qdrant_transport.py`, backend `qdrant_backend.py`, outbox `qdrant_outbox.py`.
- **Read retrieval is unconditionally quarantined** — `qdrant_backend.py:443` and `qdrant_recall_fallback.py:243` raise `PermissionError` pending authoritative policy rehydration; `recall/planner.py:120` reports the same. Index **sync** still runs (`verbatim_store.py:662`). Treat Qdrant as write/sync-only today.

**Kuzu (graph, opt-in):** embedded single-file graph DB via the `graph` extra, off unless `MEMORYMASTER_RECALL_GRAPH=1`.

## LLM Providers

Six providers registered in `_PROVIDERS` (`memorymaster/core/llm_provider.py:642`), selected by `MEMORYMASTER_LLM_PROVIDER` (default `google`, `llm_provider.py:733`):

| Provider | Entry | Credential |
|----------|-------|------------|
| `google` | `_call_google` (`llm_provider.py:164`) | `GEMINI_API_KEY`, or rotated keysets `MEMORYMASTER_LLM_API_KEYS` / `GEMINI_API_KEYS` / `MEMORYMASTER_API_KEYS`, or a file rotator |
| `openai` | `_call_openai:225` | `OPENAI_API_KEY` |
| `anthropic` | `_call_anthropic:253` | `ANTHROPIC_API_KEY` |
| `ollama` | `_call_ollama:278` | none (`OLLAMA_URL`) |
| `opencode` | `_call_opencode:304` | local `opencode` CLI |
| `claude_cli` | `_call_claude_cli:398` | local Claude CLI, probed by `_probe_claude_cli:348` |

Cross-cutting provider behaviour:
- Round-robin key rotation with cooldown (`_get_google_env_rotator:107`, `RoundRobinKeyRotator`).
- Quota-aware fallback: `MEMORYMASTER_LLM_FALLBACK_PROVIDER` fires when the primary returns a quota-shaped error (`_looks_like_quota_error:697`, `call_llm:704`); counters exposed via `get_fallback_stats:680`.
- Call-scoped env overrides via `ContextVar` (`use_call_scoped_env:45`) so concurrent calls don't stomp each other's provider config.

## Configuration

**Environment:** 218 distinct `MEMORYMASTER_*` env vars across the package. Tunables (retrieval weights, decay rates, thresholds) are centralised in `memorymaster/core/config.py`; a JSON overlay is loadable via `MEMORYMASTER_CONFIG_FILE` (`config.py:81`). Most-referenced: `MEMORYMASTER_LLM_PROVIDER`, `MEMORYMASTER_LLM_MODEL`, `MEMORYMASTER_DEFAULT_DB`, `MEMORYMASTER_WAL_DISCIPLINE`, `MEMORYMASTER_WORKSPACE`.

**Build:** `pyproject.toml` only — no setup.py, no tox, no Makefile. Package data ships `schema.sql`, `schema_postgres.sql`, `config_templates/*.md`, `config_templates/hooks/*.py` (`pyproject.toml:81-87`).

## Entry Points (console scripts, `pyproject.toml:54-62`)

`memorymaster` (CLI) · `memorymaster-mcp` (stdio MCP) · `memorymaster-mcp-http` (streamable HTTP) · `memorymaster-dashboard` · `memorymaster-steward` · `memorymaster-setup` · `memorymaster-session-end` · `memorymaster-ops`

## Platform Requirements

**Development:** Python 3.10–3.12; SQLite with FTS5 preferred (degrades gracefully without). Optional: Docker for Qdrant/Ollama/Postgres (`docker-compose.yml`, `docker-compose.postgres.yml`).

**Production / CI:** `.github/workflows/ci.yml` runs a 6-cell matrix (ubuntu-latest + windows-latest × 3.10/3.11/3.12) installing `.[dev,mcp,security,postgres]`. Downstream jobs gated on `test`:
- `perf` — `benchmarks/perf_smoke.py` against `benchmarks/slo_targets.json`, best-of-3 to absorb shared-runner noise.
- `release-truth` — `scripts/generate_release_truth.py --check` (generated release facts must be committed and current).
- `eval` — `scripts/eval_memorymaster.py`, `continue-on-error: true`.
- `deployment-smoke` — stdio MCP handshake, `docker build`, disposable `init-db`, dashboard `/healthz` + `/readyz`, authenticated streamable-HTTP MCP handshake, then `helm lint` + `helm template` for both the dashboard and `mcp-http` profiles.

`.github/workflows/publish.yml` handles release publication. Container image: root `Dockerfile`; Kubernetes chart: `helm/memorymaster/` (requires an image **digest**, not a tag).

---

*Stack analysis: 2026-08-17*
