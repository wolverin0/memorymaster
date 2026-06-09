# STACK.md — Technology Stack

*Regenerated 2026-06-09 from the current tree (v3.28.0). Supersedes the stale v2.0.0 document.*

## Language & Runtime
- **Python 3.10+** (`requires-python = ">=3.10"`, classifiers for 3.10/3.11/3.12)
- Core is **no longer zero-dependency**: mandatory deps are `requests>=2.31` and `tenacity>=8.2` (pyproject.toml `[project].dependencies`)

## Package & Version
- `pyproject.toml`: `name = "memorymaster"`, **`version = "3.28.0"`**, MIT license
- Build: **setuptools>=68** with `setuptools.build_meta`
- Package data ships `schema.sql`, `schema_postgres.sql`, `config_templates/*.md`, `config_templates/hooks/*.py`

## Storage Backends
- **SQLite** (default) — `storage.py` (`SQLiteStore`) split into `_storage_schema.py`, `_storage_read.py`, `_storage_write_claims.py`, `_storage_lifecycle.py`, `_storage_sources.py`, `_storage_shared.py`. WAL + FTS5; `PRAGMA journal_mode = WAL` and `PRAGMA busy_timeout = 5000` set on connect (storage.py:42-48)
- **PostgreSQL** (optional) — `postgres_store.py` via `psycopg[binary]>=3.2`; schema `schema_postgres.sql`; selected via `store_factory.py`
- **Qdrant** (optional vector DB) — `qdrant_backend.py` (httpx REST) + `qdrant_recall_fallback.py`
- **Kuzu** (optional embedded graph DB) — `graph_store.py`, gated by `MEMORYMASTER_RECALL_GRAPH=1`

## Schema Migrations
- **Versioned migration framework** in `memorymaster/migrations/` (shipped v3.20.0-S1, commit 2067a64): `runner.py` (`MigrationRunner`) discovers `NNNN_*.py` modules, applies per-backend (`apply_sqlite` / `apply_postgres`), records sha256 checksums in `schema_versions`, and raises `MigrationDriftError` if an applied migration's source changes
- Current migrations: `0001_initial`, `0002_miner_state`, `0003_contradiction_verdicts`, `0004_query_cache`, `0006_verbatim_session_content_index`, `0007_rule_stats` — **0005 was intentionally never shipped** (documented in 0006's docstring)

## Optional Extras (pyproject.toml)
| Extra | Packages | Purpose |
|-------|----------|---------|
| `postgres` | psycopg[binary]>=3.2 | PostgreSQL backend |
| `security` | cryptography>=42 | Sensitive-claim encryption |
| `embeddings` | sentence-transformers>=3.0 | Local semantic embeddings |
| `gemini` | google-genai>=1.0 | LLM steward (Gemini) |
| `qdrant` | httpx>=0.27 | Qdrant REST backend |
| `vector` | sentence-transformers>=3.0, qdrant-client>=1.9 | Qdrant recall fallback + `scripts/index_claims_to_qdrant.py` (384-dim CPU embeddings) |
| `graph` | kuzu>=0.4 | Kuzu graph retrieval stream (roadmap 11.3) |
| `mcp` | mcp>=1.2 | FastMCP stdio server |
| `ml` | scikit-learn>=1.3, joblib>=1.3 | Steward classifier / RL trainer |
| `dev` | pytest>=8.2, pytest-cov>=6.0 | Testing |

## Entry Points (`[project.scripts]`)
- `memorymaster` — CLI (`cli.py`, handlers split into `cli_handlers_basic.py`, `cli_handlers_curation.py`, `cli_helpers.py`)
- `memorymaster-mcp` — MCP server (`mcp_server.py`)
- `memorymaster-dashboard` — dashboard (`dashboard.py`)
- `memorymaster-steward` — LLM steward daemon (`llm_steward.py`)
- `memorymaster-setup` — hooks/MCP/cron/skills installer (`setup_hooks.py`)

## Tooling
- **ruff** — `target-version = "py310"`, `line-length = 120`, select E/F/W, ignore E501
- **mypy** — `python_version = "3.10"`, `ignore_missing_imports`, `check_untyped_defs`
- **CI** — `.github/workflows/ci.yml`: test matrix {ubuntu, windows} x {3.10, 3.11, 3.12} with `pip install -e ".[dev,mcp,security]"`, plus a `perf` job; `publish.yml` for PyPI

## Codebase Shape
- **108 top-level modules** in `memorymaster/` (flat layout), plus subpackages `jobs/` (11 job modules), `connectors/` (whatsapp), `migrations/`, `config_templates/`
