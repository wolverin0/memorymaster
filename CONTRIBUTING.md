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

**Always run the suite with `-m "not ml"`.** The `ml`-marked tests load torch /
sentence-transformers / Qdrant and randomly SIGSEGV (exit 139, "Windows fatal
exception: access violation") or hang inside real-model loads when mixed into a
full run — see the note at the top of `pytest.ini`. Run them separately with
`-m ml`.

```bash
# Run all tests -- THE canonical command
pytest tests/ -m "not ml"

# ML/vector tests, in isolation only
pytest tests/ -m ml

# Run with coverage
pytest tests/ -m "not ml" -q --cov=memorymaster --cov-report=term-missing

# Run a specific test file
pytest tests/test_backend_parity.py -q

# Run a specific test
pytest tests/test_backend_parity.py::test_parity_ingest_then_list -q
```

To see the current suite size, count it rather than trusting a number baked into
a doc:

```bash
pytest tests/ -m "not ml" --co -q | tail -1
```

A full run takes roughly 20 minutes, and long background runs tend to get killed
before finishing. Run it in alphabetical chunks in the foreground instead:

```bash
pytest "tests/test_[a-c]*.py" -m "not ml"
pytest "tests/test_[d-i]*.py" -m "not ml"
pytest "tests/test_[j-q]*.py" -m "not ml"
pytest "tests/test_[r-z]*.py" -m "not ml"
```

All tests must pass before submitting a PR. CI runs `pytest tests/ -m "not ml"`
across a matrix of ubuntu-latest and windows-latest on Python 3.10, 3.11, and
3.12 (`.github/workflows/ci.yml`).

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

Ruff and mypy are both configured in `pyproject.toml` but **neither runs in CI** —
the CI pipeline is pytest-only. Run them locally before opening a PR; nothing
else will catch a lint regression.

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

Some of these are enforced automatically: `tests/test_architecture_budgets.py`
holds hard line-count caps for `core/service.py`, `surfaces/dashboard.py` and the
extracted application modules, and a 50-line-per-function cap in those modules.

## PR Workflow

1. **Fork** the repository
2. **Create a branch** from `main`:
   ```bash
   git checkout -b feat/my-feature
   ```
3. **Make your changes** following the code style above
4. **Write tests** for new functionality (target 80%+ coverage; no threshold is
   enforced in CI)
5. **Run the full test suite**:
   ```bash
   pytest tests/ -m "not ml"
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

The package is organised into layer directories (`core/`, `stores/`, `recall/`,
`govern/`, `knowledge/`, `bridges/`, `surfaces/`). The flat modules still present
at `memorymaster/*.py` are **deprecated compatibility shims** that rebind to the
layer paths — import and edit the layer path, never the shim.

Key modules to understand before contributing:

| Module | Purpose |
|--------|---------|
| `memorymaster/core/service.py` | Core service layer -- orchestrates all operations |
| `memorymaster/stores/storage.py` | SQLite storage backend |
| `memorymaster/stores/postgres_store.py` | Postgres storage backend |
| `memorymaster/recall/retrieval.py` | Hybrid retrieval engine (FTS5 + vector + ranking) |
| `memorymaster/surfaces/cli.py` | CLI entry point (120+ subcommands) |
| `memorymaster/surfaces/mcp_server.py` | MCP server (50 tools for AI agents) |
| `memorymaster/core/config.py` | Centralized configuration (env vars + JSON) |
| `memorymaster/govern/steward.py` | Multi-probe claim validators |
| `memorymaster/govern/llm_steward.py` | LLM-powered steward with API key rotation |
| `memorymaster/knowledge/entity_graph.py` | Entity extraction and relationship tracking |
| `memorymaster/knowledge/skills.py` | Governed skill proposals, approval, and staging export |
| `memorymaster/knowledge/daily_notes.py` | Daily notes and ghost note detection |
| `memorymaster/knowledge/vault_exporter.py` | Obsidian vault export |
| `memorymaster/surfaces/dashboard.py` | HTML dashboard with SSE streaming |
| `memorymaster/core/security.py` | Auto-redaction and sensitive data handling |
| `memorymaster/core/access_control.py` | RBAC with per-agent role overrides |
| `memorymaster/recall/embeddings.py` | Embedding providers (sentence-transformers, Gemini) |
| `memorymaster/recall/qdrant_backend.py` | Qdrant vector store integration (reads currently quarantined) |
| `memorymaster/knowledge/auto_extractor.py` | LLM-powered claim extraction |
| `memorymaster/govern/auto_resolver.py` | LLM-powered conflict resolution |
| `memorymaster/recall/context_hook.py` | Pre/post-turn context injection hooks |

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
