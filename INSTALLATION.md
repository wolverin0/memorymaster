# Installation Guide

## Requirements

- Python 3.10 or higher
- pip 23+

## pip Install

### Minimal (SQLite only, zero dependencies)

```bash
pip install memorymaster
```

### With optional extras

```bash
# MCP server for Claude Code / Codex
pip install "memorymaster[mcp]"

# Postgres backend
pip install "memorymaster[postgres]"

# Vector embeddings (sentence-transformers)
pip install "memorymaster[embeddings]"

# Gemini embeddings / LLM
pip install "memorymaster[gemini]"

# Qdrant vector store
pip install "memorymaster[qdrant]"

# Fernet encryption for sensitive payloads
pip install "memorymaster[security]"

# Everything
pip install "memorymaster[mcp,postgres,embeddings,gemini,qdrant,security]"

# Development (includes pytest)
pip install -e ".[dev,mcp,security,embeddings,qdrant]"
```

### Initialize the database

```bash
memorymaster --db memory.db init-db
```

## Docker Compose

The included `docker-compose.yml` runs the full stack: MemoryMaster + Qdrant + Ollama.

```bash
# Clone the repo
git clone https://github.com/wolverin0/memorymaster.git
cd memorymaster

# Start all services
docker compose up -d

# Verify
docker compose ps
curl http://localhost:8765/health
```

### Services

| Service | Port | Description |
|---------|------|-------------|
| `memorymaster` | 8765 | MCP server + dashboard |
| `qdrant` | 6333, 6334 | Vector store (REST + gRPC) |
| `ollama` | 11434 | Local LLM inference |

### Postgres variant

For Postgres instead of SQLite:

```bash
docker compose -f docker-compose.postgres.yml up -d
```

### Data persistence

All data is persisted in Docker volumes:
- `./data/` -- MemoryMaster database and workspace
- `qdrant_data` -- Qdrant vector storage
- `ollama_data` -- Ollama model cache

## Helm Chart (Kubernetes)

A Helm chart is included for Kubernetes deployments:

```bash
# Install from local chart
helm install memorymaster ./helm/memorymaster

# With custom values
helm install memorymaster ./helm/memorymaster \
  --set env.QDRANT_URL=http://qdrant.default.svc:6333 \
  --set env.OLLAMA_URL=http://ollama.default.svc:11434 \
  --set persistence.size=5Gi

# Upgrade
helm upgrade memorymaster ./helm/memorymaster
```

### Default Helm values

| Key | Default | Description |
|-----|---------|-------------|
| `replicaCount` | 1 | Number of replicas |
| `image.repository` | memorymaster | Container image |
| `image.tag` | latest | Image tag |
| `service.type` | ClusterIP | Kubernetes service type |
| `service.port` | 8765 | Service port |
| `persistence.enabled` | true | Enable PVC for data |
| `persistence.size` | 1Gi | PVC size |
| `env.MEMORYMASTER_DEFAULT_DB` | /data/memorymaster.db | Database path |
| `env.QDRANT_URL` | http://qdrant:6333 | Qdrant endpoint |
| `env.OLLAMA_URL` | http://ollama:11434 | Ollama endpoint |
| `resources.limits.cpu` | 500m | CPU limit |
| `resources.limits.memory` | 512Mi | Memory limit |

## MCP Server Configuration

### Claude Code

Add to your project's `.mcp.json` (see `.mcp.json.example`):

```json
{
  "mcpServers": {
    "memorymaster": {
      "command": "memorymaster-mcp",
      "env": {
        "MEMORYMASTER_DEFAULT_DB": "/path/to/memorymaster.db",
        "MEMORYMASTER_WORKSPACE": "/path/to/your/project",
        "QDRANT_URL": "http://localhost:6333",
        "OLLAMA_URL": "http://localhost:11434"
      }
    }
  }
}
```

### With Qdrant MCP server

For direct vector search alongside MemoryMaster:

```json
{
  "mcpServers": {
    "memorymaster": {
      "command": "memorymaster-mcp",
      "env": {
        "MEMORYMASTER_DEFAULT_DB": "/path/to/memorymaster.db",
        "MEMORYMASTER_WORKSPACE": "/path/to/your/project"
      }
    },
    "qdrant": {
      "command": "uvx",
      "args": ["mcp-server-qdrant"],
      "env": {
        "QDRANT_URL": "http://localhost:6333",
        "COLLECTION_NAME": "agent-memories"
      }
    }
  }
}
```

## Environment Variables

All environment variables are documented in [`.env.example`](.env.example). Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORYMASTER_DEFAULT_DB` | `memorymaster.db` | SQLite database path |
| `MEMORYMASTER_WORKSPACE` | `.` | Workspace root for file watchers |
| `MEMORYMASTER_CONFIG_FILE` | (none) | JSON config file path |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama LLM endpoint |
| `QDRANT_URL` | (none) | Qdrant vector store endpoint |
| `GEMINI_API_KEY` | (none) | Google Gemini API key |
| `MEMORYMASTER_API_KEYS` | (none) | Comma-separated LLM API keys |
| `EXTRACTOR_LLM_MODEL` | `llama3.2` | Model for claim extraction |
| `RESOLVER_LLM_MODEL` | `llama3.2` | Model for conflict resolution |
| `ENTITY_LLM_MODEL` | `llama3.2` | Model for entity extraction |

See `.env.example` for the full list including retrieval weights, decay rates, and threshold tuning.

## Entry Points

MemoryMaster installs four CLI entry points:

| Command | Description |
|---------|-------------|
| `memorymaster` | Main CLI with 50+ subcommands |
| `memorymaster-mcp` | MCP server for AI agent integration |
| `memorymaster-dashboard` | Standalone dashboard server |
| `memorymaster-steward` | Standalone LLM steward process |

## Troubleshooting

### "No module named memorymaster"

Ensure you installed memorymaster in the active Python environment:

```bash
pip install memorymaster
python -c "import memorymaster; print('OK')"
```

### MCP server not connecting

1. Verify the MCP server starts: `memorymaster-mcp`
2. Check the database path in your `.mcp.json` is absolute
3. Ensure `MEMORYMASTER_DEFAULT_DB` points to an initialized database

### Qdrant connection refused

1. Verify Qdrant is running: `curl http://localhost:6333/healthz`
2. Check the `QDRANT_URL` environment variable
3. If using Docker Compose, ensure the `qdrant` service is healthy

### Ollama not responding

1. Verify Ollama is running: `curl http://localhost:11434/api/tags`
2. Pull the required model: `ollama pull llama3.2`
3. Check the `OLLAMA_URL` environment variable

### Database locked errors

SQLite allows only one writer at a time. For concurrent access:
- Use the Postgres backend: `pip install "memorymaster[postgres]"`
- Or ensure only one process writes to the database at a time

### Tests failing after install

```bash
# Ensure dev dependencies are installed
pip install -e ".[dev]"

# Run tests
pytest tests/ -q

# If specific tests fail, check for missing optional dependencies
pip install -e ".[dev,mcp,security,embeddings,qdrant]"
```
