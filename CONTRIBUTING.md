# Contributing to MemoryMaster

Thank you for your interest in contributing to MemoryMaster. This guide covers dev setup, testing, code style, and the PR workflow.

## Dev Setup

```bash
# Clone the repository
git clone https://github.com/wolverin0/memorymaster.git
cd memorymaster

# Install in editable mode with all dev extras
pip install -e ".[dev,mcp,security,embeddings,qdrant]"

# Initialize a local database for testing
memorymaster --db test.db init-db
```

### Optional services

For full-stack development with vector search and LLM features:

```bash
# Start Qdrant and Ollama via Docker Compose
docker compose up -d qdrant ollama
```

## Testing

MemoryMaster has 932 tests across 66 test modules:

```bash
# Run all tests
pytest tests/ -q

# Run with coverage
pytest tests/ -q --cov=memorymaster --cov-report=term-missing

# Run a specific test file
pytest tests/test_service.py -q

# Run a specific test
pytest tests/test_service.py::test_ingest_and_query -q
```

All tests must pass before submitting a PR. The CI pipeline runs the full suite on Python 3.10, 3.11, and 3.12.

## Code Style

We use [ruff](https://docs.astral.sh/ruff/) for linting and formatting:

```bash
# Check for lint issues
ruff check memorymaster/

# Auto-fix lint issues
ruff check memorymaster/ --fix

# Format code
ruff format memorymaster/
```

Key style rules:
- **Line length**: 120 characters (E501 ignored, handled by formatter)
- **Target version**: Python 3.10
- **Lint rules**: E, F, W (pycodestyle errors, pyflakes, pycodestyle warnings)
- **Immutability**: Create new objects, never mutate existing ones
- **Functions**: Keep under 50 lines
- **Files**: Keep under 800 lines
- **Nesting**: Max 4 levels deep
- **No `console.log` / `print` in production code** (use proper logging)
- **No hardcoded secrets**: Use environment variables

## PR Workflow

1. **Fork** the repository
2. **Create a branch** from `main`:
   ```bash
   git checkout -b feat/my-feature
   ```
3. **Make your changes** following the code style above
4. **Write tests** for new functionality (target 80%+ coverage)
5. **Run the full test suite**:
   ```bash
   pytest tests/ -q
   ruff check memorymaster/
   ```
6. **Commit** with conventional commit messages:
   ```
   feat: add support for new connector
   fix: correct decay rate calculation for high-volatility claims
   refactor: extract validator logic into separate module
   docs: update API endpoint documentation
   test: add integration tests for steward probes
   chore: update dependencies
   ```
7. **Push** and open a PR against `main`

## Architecture Overview

Key modules to understand before contributing:

| Module | Purpose |
|--------|---------|
| `memorymaster/service.py` | Core service layer -- orchestrates all operations |
| `memorymaster/storage.py` | SQLite storage backend |
| `memorymaster/postgres_store.py` | Postgres storage backend |
| `memorymaster/retrieval.py` | Hybrid retrieval engine (FTS5 + vector + ranking) |
| `memorymaster/cli.py` | CLI entry point (50+ subcommands) |
| `memorymaster/mcp_server.py` | MCP server (21 tools for AI agents) |
| `memorymaster/config.py` | Centralized configuration (env vars + JSON) |
| `memorymaster/steward.py` | Multi-probe claim validators |
| `memorymaster/llm_steward.py` | LLM-powered steward with API key rotation |
| `memorymaster/entity_graph.py` | Entity extraction and relationship tracking |
| `memorymaster/skill_evolver.py` | Skill evolution from accumulated knowledge |
| `memorymaster/daily_notes.py` | Daily notes and ghost note detection |
| `memorymaster/vault_exporter.py` | Obsidian vault export |
| `memorymaster/dashboard.py` | HTML dashboard with SSE streaming |
| `memorymaster/security.py` | Auto-redaction and sensitive data handling |
| `memorymaster/access_control.py` | RBAC with per-agent role overrides |
| `memorymaster/embeddings.py` | Embedding providers (sentence-transformers, Gemini) |
| `memorymaster/qdrant_backend.py` | Qdrant vector store integration |
| `memorymaster/auto_extractor.py` | LLM-powered claim extraction |
| `memorymaster/auto_resolver.py` | LLM-powered conflict resolution |
| `memorymaster/context_hook.py` | Pre/post-turn context injection hooks |

For detailed system design, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Reporting Issues

When filing a bug report, please include:

1. Python version (`python --version`)
2. MemoryMaster version (`memorymaster --version`)
3. Steps to reproduce
4. Expected vs actual behavior
5. Relevant log output or error messages

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
