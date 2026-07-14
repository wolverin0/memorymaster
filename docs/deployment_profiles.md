# Deployment Profiles (D4)

This document defines practical deployment profiles for MemoryMaster with reliability/operability defaults.

## Product Posture

Profile A is the primary product: one person, one local SQLite database, and a
private stdio MCP process. Profiles B and C are deferred design options, not
current release targets. Their presence documents fail-closed boundaries; it
does not imply that users should provision Postgres, containers, or shared
infrastructure.

Qdrant is a separate optional semantic enhancement and is not part of Profile
A unless the user explicitly selects and verifies that profile.

## Secure Container Inputs

Container configuration fails closed when required deployment inputs are
missing. Keep real values in an operator-controlled secret store or a local
gitignored `.env`, never in Compose, Helm values, shell history, or source.

- `docker-compose.postgres.yml` requires
  `MEMORYMASTER_POSTGRES_PASSWORD`, binds PostgreSQL to `127.0.0.1`, and checks
  health with an authenticated `SELECT 1`.
- `docker-compose.yml` requires `QDRANT_API_KEY` plus externally verified
  `QDRANT_IMAGE_DIGEST` and `OLLAMA_IMAGE_DIGEST` values in
  `sha256:<64-hex>` form. Repositories are fixed in Compose so a mutable tag
  cannot be substituted. Qdrant and Ollama host ports bind to `127.0.0.1` only.
- The Helm chart requires `image.digest` and an existing Secret named through
  `qdrant.apiKeySecret.name`. It never accepts a literal Qdrant API key. Set
  `qdrant.caSecret.name` to mount an optional trusted CA as
  `QDRANT_CA_CERT`.

Validate interpolation before any runtime action:

```powershell
docker compose -f docker-compose.postgres.yml config
docker compose -f docker-compose.yml config
```

The Compose and Helm profiles still inherit the R3.4 stdio-versus-HTTP
entrypoint/readiness blocker. Configuration validation is not runtime or
deployment evidence; do not expose or promote these profiles until that
separate gate is resolved.

## Profile A: Local Developer (SQLite)

Use when:
- single developer
- local experiments, test loops, feature validation

Runtime:
- DB: local SQLite file (`memorymaster.db`)
- process: CLI/operator loop on same host
- MCP auth: `MEMORYMASTER_MCP_AUTH_MODE=local-trusted` in a private stdio process
- PostgreSQL is not supported in this profile; use Profile B/C with team authority

Recommended commands:
```powershell
python -m memorymaster --db memorymaster.db init-db
python -m memorymaster --db memorymaster.db run-operator --inbox-jsonl .\turns.jsonl --retrieval-mode hybrid --policy-mode cadence --max-idle-seconds 120 --log-jsonl artifacts\operator\operator_events.jsonl
```

Reliability notes:
- keep operator checkpoints enabled (`artifacts/operator/operator_state.json`)
- run synthetic eval + perf smoke before sharing changes:
```powershell
python scripts/eval_memorymaster.py --strict
python benchmarks/perf_smoke.py
```

## Deferred Profile B: Small Team Server (Postgres)

Revisit only when:
- 2-10 engineers
- one shared service host
- moderate throughput

Runtime:
- DB: Postgres for concurrency/durability
- process model: one long-running operator, optional dashboard process
- MCP auth: `team`, with a tenant, authenticated principal, non-wildcard scope allowlist, and restricted application DSN
- schema lifecycle: separate migrator DSN; the application runtime cannot initialize or migrate

Baseline controls:
- supply the PostgreSQL password out of band; never restore the retired fixed
  Compose credential
- backup policy for DB and artifacts
- health endpoint checks (`/health`)
- distinct migrator/application roles meeting the contract below
- team action proposals and raw merge/sync disabled
- periodic reconciliation report:
  - `service.store.reconcile_integrity(fix=False)` in scheduled job

## Deferred Profile C: Cloud Managed DB (Postgres)

Revisit only when:
- production workloads
- multi-service/multi-agent concurrency
- stricter recovery and audit requirements

Runtime:
- DB: managed Postgres with TLS + backups + PITR
- application nodes: stateless operator workers
- observability: centralized logs + metric exporter (future D3)
- identity: each runtime request binds tenant, principal, and explicit scopes transaction-locally

Recommended controls:
- rotate credentials and isolate DB role permissions
- keep migrator credentials out of application nodes and MCP configuration
- scheduled integrity reconciliation (`report` daily, `fix` only with review)
- retain artifacts for audit windows (`artifacts/eval`, `artifacts/perf`, `artifacts/e2e`)

## PostgreSQL Role and RLS Contract

This contract currently targets PostgreSQL 16.x. Treat other major versions as
unverified until their catalog and privilege matrix passes the disposable gate.

Provision two purpose-specific secrets:

- Migrator: distinct schema owner with `SUPERUSER` or `BYPASSRLS`, used only for
  initialization/versioned migrations. FORCE RLS means an ordinary owner alone
  is not a sufficient migration boundary.
- Application: non-owner with
  `NOSUPERUSER NOBYPASSRLS NOREPLICATION NOCREATEROLE NOCREATEDB`; no ability to
  `SET ROLE` into a superuser/BYPASSRLS role; no schema `CREATE`; no table
  `TRUNCATE`, `REFERENCES`, or `TRIGGER`; no DDL/migration rights; no DML on the
  deny-only governance/raw-ingest tables; `SELECT`-only on `cache_meta` and
  `schema_versions`; `SELECT` + `INSERT` but no table/column `UPDATE` and no
  `DELETE` on append-only `events`; and
  explicit `EXECUTE` only on the hash-only
  `public.memorymaster_event_chain_head()` function (never through `PUBLIC`).

Migration v0011 enables and forces RLS on all 15 protected tables. Scoped tables
use exact command-specific permissive/restrictive policy pairs. Reads are
tenant/scope-bound and return public claims or the principal's own private
claims. Writes to claims and claim-owned rows are owner-only and limited to
public/private visibility; every team claim, including a public claim, requires
a nonblank `source_agent` owner. `mcp_usage` and
claimless audit events remain tenant/principal-bound. Action proposals, Atlas
source/evidence tables, media retry, query cache, miner state, and rule stats are
deny-only. A tenant-derived hash-only function preserves the event chain across
private principals/scopes without exposing payloads. PostgreSQL cache-generation
triggers are dropped because query-cache metadata is not writable in team runtime.

Migration v0012 makes public idempotency keys, human IDs, and confirmed tuples
tenant + exact-scope local. Non-public identities additionally include exact
visibility and principal. Startup requires an exact six-index catalog with no
extra non-primary unique claim indexes, checksum-frozen v0011/v0012 migrations,
the validated claim-owner constraint, exact policy expressions/commands/roles,
the exact event-head function (owned by `SUPERUSER`/`BYPASSRLS`) and append-only
trigger/privilege contract, and the
restricted role catalog. Existing rows with blank/null owners (public included),
noncanonical visibility, or namespace duplicates require a read-only inventory.
The exact claims-trigger catalog rejects self- and cross-tenant/scope/visibility/
owner supersession links, including boundary-field changes. The canonical
lifecycle locks both rows and commits reciprocal pointers plus one event
atomically. Existing invalid or nonreciprocal supersession edges are part of the
read-only preflight. Owner backfill, duplicate/supersession-edge remediation, and
constraint validation require
explicit approval plus an approved backup and maintenance window.

The local-trusted profile remains SQLite-only. Team action proposals and raw
merge/sync are not enabled by selecting PostgreSQL.

## Disposable Integration Gate

Real PostgreSQL verification requires a database whose full lifecycle is
disposable and all three variables below:

```text
MEMORYMASTER_TEST_POSTGRES_DSN=<dedicated migrator DSN>
MEMORYMASTER_TEST_POSTGRES_APP_DSN=<distinct restricted app DSN>
MEMORYMASTER_TEST_POSTGRES_RLS_DISPOSABLE=1
```

Run `python -m pytest tests/test_postgres_rls_integration.py -q`. Until that
two-role test passes, catalog behavior, cross-tenant read/write denial, event
owner/grants, and atomic supersession remain `BLOCKED-EXTERNAL`; unit/fake
results are not production evidence.
Brownfield inventory/backfill/constraint-validation evidence is a separate
external operator action and must never be inferred from the disposable test.

## Rollout Checklist

1. Initialize/migrate with the dedicated migrator DSN; remove it from runtime nodes.
2. Verify the application role contract, FORCE RLS, exact
   policy/event/supersession/identity catalogs, the validated owner constraint,
   append-only event privileges, and canonical supersession transaction.
3. Validate team connectivity with tenant, principal, and explicit scopes.
4. Run the disposable two-DSN integration gate before any production rollout.
5. Run operator smoke against representative non-product inbox rows.
6. Run `scripts/eval_memorymaster.py --strict` and `benchmarks/perf_smoke.py`.
7. Capture reconciliation report and confirm zero critical findings before go-live.
