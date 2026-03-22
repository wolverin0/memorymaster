# STACK.md — Technology Stack

## Language & Runtime
- **Python 3.10+** (production-stable)
- **No mandatory external dependencies** (zero-dep core)

## Storage Backends
- **SQLite** (default) — `memorymaster/storage.py` via `SQLiteStore`
- **PostgreSQL** (optional) — `memorymaster/postgres_store.py` via `psycopg[binary]>=3.2`
- **Qdrant** (optional vector DB) — `memorymaster/qdrant_backend.py` via `httpx>=0.27`
- Store selected at runtime via `store_factory.py`

## Optional Extras (pyproject.toml)
| Extra | Package | Purpose |
|-------|---------|---------|
| `postgres` | psycopg[binary]>=3.2 | PostgreSQL backend |
| `security` | cryptography>=42 | Sensitive-claim encryption |
| `embeddings` | sentence-transformers>=3.0 | Local semantic embeddings |
| `gemini` | google-genai>=1.0 | LLM steward (Gemini) |
| `qdrant` | httpx>=0.27 | Qdrant vector backend |
| `mcp` | mcp>=1.2 | Model Context Protocol server |
| `dev` | pytest>=8.2 | Testing |

## Entry Points
- `memorymaster` — CLI (`cli.py`)
- `memorymaster-mcp` — MCP server (`mcp_server.py`)
- `memorymaster-dashboard` — Dashboard TUI (`dashboard.py`)
- `memorymaster-steward` — LLM steward daemon (`llm_steward.py`)

## Build System
- **setuptools>=68** with `setuptools.build_meta`
- Package version: `2.0.0`
- Includes SQL schema files via `package-data`
