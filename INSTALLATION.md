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

# Qdrant maintenance-index client (payload retrieval is quarantined in R1.3)
pip install "memorymaster[qdrant]"

# Fernet encryption for sensitive payloads
pip install "memorymaster[security]"

# Everything
pip install "memorymaster[mcp,postgres,embeddings,gemini,qdrant,security]"

# Development (matches CI — required extras for the full test suite)
pip install -e ".[dev,mcp,security]"
```

### Initialize a local SQLite database

```bash
memorymaster --db memory.db init-db
```

Local-trusted operation is SQLite-only. PostgreSQL initialization is a separate
team-deployment step performed with a dedicated migrator DSN; the PostgreSQL
application role cannot initialize or migrate the schema.

### Setup (hooks + MCP + cron)

After `pip install memorymaster`, run the installer to wire MemoryMaster into
Claude Code (hooks, MCP server, steward cron) and optionally Codex.

**Recommended — let your agent drive it** (paste [`docs/AGENT-INSTALL.md`](docs/AGENT-INSTALL.md)
into Claude Code or Codex):

```bash
memorymaster-setup --yes --full-stack --json
```

**Manual / interactive:**

```bash
memorymaster-setup
```

Running from a cloned repo? `python scripts/setup-hooks.py` also works — it is
a 3-line shim that calls the same `memorymaster.surfaces.setup_hooks:main` function.

#### `memorymaster-setup` flags

| Flag | Default | Description |
|------|---------|-------------|
| `-y` / `--yes` | off | Non-interactive; accept all defaults (no `input()` prompts) |
| `--db PATH` | `<project-root>/memorymaster.db` | Path to the SQLite database |
| `--provider {google,openai,anthropic,ollama}` | prompted | LLM provider for the auto-ingest Stop hook |
| `--api-key KEY` | prompted | API key for the chosen provider |
| `--model MODEL` | provider default | LLM model id |
| `--project-root PATH` | cwd | Directory where `memorymaster.db` lives |
| `--full-stack` | on | Bring up the Qdrant maintenance index + Ollama via Docker Compose |
| `--no-full-stack` | off | Skip the Qdrant-index + local-LLM stack |
| `--no-cron` | off | Skip steward cron setup |
| `--no-obsidian-skills` | off | Skip Obsidian skills install |
| `--codex` | auto-detect | Force Codex MCP + instructions wiring |
| `--no-codex` | — | Skip Codex wiring |
| `--force` | off | Overwrite existing MCP entries (default: skip if present) |
| `--verify-only` | off | Run only the sentinel round-trip verify and exit |
| `--json` | off | Emit machine-readable JSON result on stdout (human chatter goes to stderr) |

#### What the installer does

- Probes your environment first (Python, Docker, Qdrant, Ollama, `~/.claude/`, `~/.codex/`) and prints a plan; existing components are reused, not overwritten.
- Copies 7 hook scripts to `~/.claude/hooks/` and wires them into `~/.claude/settings.json`: recall, classify, validate-wiki, session-start, auto-ingest, pre-compact, plus the 6-hour steward cron.
- Registers the `memorymaster` MCP server globally in `~/.claude.json` (using `memorymaster.surfaces.mcp_server` — not the deprecated path).
- Optionally appends Codex `AGENTS.md` + global `CLAUDE.md` integration snippets.
- Optionally installs the steward cron (Linux/macOS) or Task Scheduler job (Windows).
- Runs a sentinel round-trip verify at the end (`--verify-only` to run this step alone).

#### No-Docker degraded mode

If Docker is absent and Qdrant/Ollama are not already running, the installer
continues without them and prints:

```
Running in SQLite-only mode. Qdrant index maintenance + local LLM auto-ingest are OFF.
  Retrieval remains available through authoritative SQLite ranking. To enable index
  maintenance or local LLMs, use --full-stack or QDRANT_URL / OLLAMA_URL.
```

Setup exits 0. Core hooks, MCP, and SQLite-based recall all work normally in
degraded mode. Installing or starting Qdrant enables only index maintenance
during R1.3; it does not re-enable claim, context-fallback, or verbatim payload
retrieval.

## Docker Compose

The included `docker-compose.yml` runs MemoryMaster plus the optional Qdrant
maintenance index and Ollama. Qdrant payload retrieval remains quarantined.

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
| `qdrant` | 6333, 6334 | Maintenance index (REST + gRPC); no claim/verbatim payload retrieval in R1.3 |
| `ollama` | 11434 | Local LLM inference |

### Postgres variant

The included Postgres Compose file is a development scaffold, not a secure team
deployment by itself:

```bash
docker compose -f docker-compose.postgres.yml up -d
```

Before using it with MemoryMaster, replace any example credential, keep the
database port private, and provision the distinct migrator and application roles
defined in [PostgreSQL team runtime security boundary](#postgresql-team-runtime-security-boundary).

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
        "MEMORYMASTER_MCP_AUTH_MODE": "local-trusted",
        "QDRANT_URL": "http://localhost:6333",
        "OLLAMA_URL": "http://localhost:11434"
      }
    }
  }
}
```

This `local-trusted` example is SQLite-only. A PostgreSQL team runtime uses an
explicit authority envelope and the restricted application DSN:

```json
{
  "mcpServers": {
    "memorymaster": {
      "command": "memorymaster-mcp",
      "env": {
        "MEMORYMASTER_MCP_AUTH_MODE": "team",
        "MEMORYMASTER_MCP_PRINCIPAL": "agent-id",
        "MEMORYMASTER_ROLE_AGENT_ID": "writer",
        "MEMORYMASTER_MCP_TENANT_ID": "tenant-id",
        "MEMORYMASTER_MCP_WORKSPACE": "/absolute/path/to/workspace",
        "MEMORYMASTER_MCP_ALLOWED_SCOPES": "project:example,global",
        "MEMORYMASTER_MCP_DB": "postgresql://app-role:password@host/database"
      }
    }
  }
}
```

All team values are required, including an explicit `admin`, `writer`, or
`reader` mapping for the principal (`MEMORYMASTER_ROLE_<AGENT>`; use underscores
for hyphens in the environment-key suffix). Scope wildcards and caller-supplied
authority widening are rejected. Do not put the migrator DSN in an MCP
configuration.

### PostgreSQL team runtime security boundary

The hardened team profile currently targets PostgreSQL 16.x; other major
versions remain unverified because their role/table privilege catalogs differ.
PostgreSQL is supported only as an authenticated team application runtime.
Keep two purpose-specific DSNs in separate secrets:

- **Migrator DSN:** a dedicated schema-owning role with `SUPERUSER` or
  `BYPASSRLS`, used only for `init-db` and versioned migrations. FORCE RLS makes
  a plain table owner subject to policy, so schema ownership alone is not enough.
- **Application DSN:** a distinct non-owner role used by MCP/services. It must be
  `NOSUPERUSER NOBYPASSRLS NOREPLICATION NOCREATEROLE NOCREATEDB`, must not be
  able to `SET ROLE` into a superuser/BYPASSRLS role, and must not own protected
  tables or their owner role.

The application role also must not have schema `CREATE`, table `TRUNCATE`,
`REFERENCES`, or `TRIGGER`, DDL/migration rights, or DML on the deny-only
governance/raw-ingest tables. Grant it only the DML needed by the scoped runtime,
the corresponding sequence privileges, and `SELECT` (not write) on
`cache_meta` and `schema_versions`. The event ledger is append-only: the
application role requires `SELECT` and `INSERT`, but must have no table- or
column-level `UPDATE` and no `DELETE` privilege on `events`. Grant `EXECUTE` only on
`public.memorymaster_event_chain_head()`; v0011 revokes that capability from
`PUBLIC`. The function derives its tenant from transaction-local authority and
returns only ledger head hashes, never event payloads. Its SECURITY DEFINER
owner must be `SUPERUSER` or `BYPASSRLS`; startup rejects an ordinary owner that
would see a FORCE-RLS-filtered partial head. Team action proposals
and raw merge/sync remain disabled; run reviewed administration through the
separate migrator/maintenance boundary.

Migration v0011 enables and forces RLS on all 15 protected tables. Each scoped
table receives exact command-specific permissive/restrictive policy pairs:

- claim reads require tenant + explicit scope and expose public rows or the
  authenticated principal's own private rows;
- claim and claim-owned child writes require tenant + scope + principal
  ownership, including both endpoints of links/verdicts, require a nonblank
  `source_agent` owner on every claim (including public claims), and accept only
  public/private visibility in team runtime;
- claimless audit events remain tenant/principal bound; claim events inherit the
  referenced claim's read/write boundary, while the hash-only event-head
  function prevents private/scope RLS from forking the tenant ledger;
- `mcp_usage` is tenant/principal bound;
- action proposals, Atlas source/evidence tables, media retry, query cache,
  miner state, and rule stats are deny-only in team runtime.

Migration v0012 replaces the three tenant-global identity constraints with six
partial unique indexes. Public idempotency keys, human IDs, and confirmed tuples
use exact tenant + scope namespaces. Non-public identities additionally use
exact visibility + `source_agent`. Runtime startup verifies that the complete
non-primary unique-index catalog is exactly those six definitions; missing,
extra, invalid, nonunique, or differently defined claim identity indexes fail
closed. It also verifies the checksums of v0011 and v0012 before binding
authority. Direct human-ID/idempotency-key reads without an exact scope are
accepted only when one visible row exists; ambiguity fails closed.

v0012 also installs `trg_claims_supersession_boundary`. It rechecks references
when either pointer or any tenant/scope/visibility/owner boundary field changes,
and denies self- or cross-boundary links without revealing the hidden target.
`mark_superseded()` locks both claims and writes the old status/pointer, the
replacement's reciprocal pointer, and one supersession event in one transaction.
The legacy `set_supersedes()` compatibility method delegates to the same atomic
path.

Before applying v0012 to an existing deployment, perform a read-only inventory
of noncanonical visibility values, blank/null `source_agent` owners on all claim
rows (including public rows), and duplicate identities inside the tenant + exact
scope namespaces. Also inventory self-linked, missing-target, nonreciprocal, or
cross-tenant/scope/visibility/owner `supersedes_claim_id` and
`replaced_by_claim_id` edges. Do not mutate product data as part of that
inventory. v0012 performs this supersession preflight read-only and refuses DDL
when invalid edges exist.
PostgreSQL adds `ck_claims_identity_visibility_owner` as `NOT VALID` and then
validates it in the same migration. A brownfield migration therefore refuses to
complete while any ownerless row remains; team application startup also rejects
a missing, altered, or unvalidated constraint. Backfilling owners and resolving
duplicates are product-data maintenance actions that require explicit approval,
an approved backup, and a reviewed maintenance window before rerunning v0012.

The migration also removes the PostgreSQL query-cache generation triggers
(`claims_gen_ins_del`, `claims_gen_upd`) because team runtime cannot safely write
the read-only cache metadata. A runtime connection validates its role, table
ownership/privileges, required event `SELECT`/`INSERT`, the absence of table- or
column-level event `UPDATE` and event `DELETE`, FORCE RLS, literal-sensitive
command/role/expression policy definitions, the exact event-head function
signature/body/security/owner settings, exact event and claims trigger catalogs,
the validated claim-owner constraint, the exact six
identity indexes, and transaction-local tenant, principal, and scope settings
before returning the connection.

#### Disposable PostgreSQL RLS verification

Real catalog and policy behavior is intentionally opt-in. Supply both DSNs for
the same disposable database and the explicit opt-in, then run the integration
module:

```bash
export MEMORYMASTER_TEST_POSTGRES_DSN='postgresql://migrator:...@host/test_database'
export MEMORYMASTER_TEST_POSTGRES_APP_DSN='postgresql://app-role:...@host/test_database'
export MEMORYMASTER_TEST_POSTGRES_RLS_DISPOSABLE=1
python -m pytest tests/test_postgres_rls_integration.py -q
```

The test refuses identical roles and known live DSN variables. Never target
product data. Until this two-DSN suite passes in a real PostgreSQL environment,
the runtime proof and restricted-grant evidence remain `BLOCKED-EXTERNAL`;
fake/catalog unit tests are not a substitute. Brownfield read-only inventory,
owner backfill, duplicate remediation, and constraint validation are separately
blocked pending explicit operator approval and are recorded in
`external-actions-required.md`.

### Standalone Qdrant MCP server (not a MemoryMaster retrieval path)

The example below exposes a separate, third-party Qdrant MCP server. It is not
a supported way to query MemoryMaster's index during R1.3: doing so would bypass
MemoryMaster lifecycle, tenant, scope, visibility, and sensitivity policy. Do
not point it at a MemoryMaster collection. Keep MemoryMaster queries on the
`memorymaster` MCP server, where Qdrant claim requests use lexical fallback and
team semantic requests are denied.

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
| `MEMORYMASTER_MCP_AUTH_MODE` | (required) | `local-trusted` (SQLite only) or `team` |
| `MEMORYMASTER_MCP_PRINCIPAL` | (none) | Required authenticated principal in team mode |
| `MEMORYMASTER_ROLE_<AGENT>` | (none) | Required explicit `admin`, `writer`, or `reader` mapping for each team principal |
| `MEMORYMASTER_MCP_TENANT_ID` | (none) | Required tenant in team mode |
| `MEMORYMASTER_MCP_ALLOWED_SCOPES` | (none) | Required explicit comma-separated team scope allowlist |
| `MEMORYMASTER_MCP_DB` | (none) | Restricted application DSN/path for team mode |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama LLM endpoint |
| `QDRANT_URL` | (none) | Qdrant maintenance-index endpoint for upsert/sync/reconcile; does not enable payload retrieval |
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

This matters only for upsert, sync, reconcile, count/ID drift checks, and other
index maintenance; authoritative retrieval continues without Qdrant.

1. Verify Qdrant is running: `curl http://localhost:6333/healthz`
2. Check the `QDRANT_URL` environment variable
3. If using Docker Compose, ensure the `qdrant` service is healthy

### Ollama not responding

1. Verify Ollama is running: `curl http://localhost:11434/api/tags`
2. Pull the required model: `ollama pull llama3.2`
3. Check the `OLLAMA_URL` environment variable

### Database locked errors

SQLite allows only one writer at a time. For concurrent access:
- Configure the authenticated [PostgreSQL team runtime security boundary](#postgresql-team-runtime-security-boundary); installing `memorymaster[postgres]` alone is insufficient
- Or ensure only one process writes to the database at a time

### Tests failing after install

```bash
# Install the same dependency set that CI uses — this is what the
# public GitHub Actions workflow runs against, so it's the canonical
# tested configuration:
pip install -e ".[dev,mcp,security]"

# Run tests
pytest tests/ -q

# If you want to additionally exercise optional embeddings, Qdrant
# maintenance, and payload-read containment paths (which are skipped via
# pytest.importorskip when dependencies are absent), install their extras too:
pip install -e ".[dev,mcp,security,embeddings,qdrant]"
```

> **Note**: CI (`.github/workflows/ci.yml`) installs `.[dev,mcp,security]`
> only. Optional embeddings/Qdrant tests skip automatically if those
> extras are not present, so the smaller install is the supported
> reproduction environment.
