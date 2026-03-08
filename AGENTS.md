# MemoryMaster Usage Policy

This project uses MemoryMaster as the shared memory backend for all Codex sessions.

## Required Workflow Per Task

1. At task start, call `query_memory` with a short query about the user goal and current context.
2. During task execution, call `ingest_claim` for durable facts that matter later (paths, endpoints, decisions, credentials location hints, constraints, owner decisions).
3. Before final response, call `run_cycle` once (default cadence policy is acceptable).
4. After final response, ingest a compact task summary using `ingest_claim`.

## Defaults

- Use MCP tool defaults for `db` and `workspace` unless an explicit override is required.
- Shared DB is configured globally via MCP env (`MEMORYMASTER_DEFAULT_DB`).
- Hybrid scope model is enabled in MCP server:
  - Ingest default `scope="project"` auto-resolves to a project-specific scope key derived from workspace path.
  - Query default scope allowlist (when omitted) is: project-specific scope + `global` (+ legacy `project`).
  - This gives per-project isolation with optional global recall.

## Scope Rules

- Use `scope="global"` only for facts intentionally shared across projects.
- Keep project-specific facts on default scope (do not force `global`).
- To query only one project, pass `scope_allowlist="<project-scope>"` explicitly.

## Guardrails

- Do not store secrets in plain text unless the user explicitly asks; prefer redacted references.
- Prefer high-signal facts over noisy conversational content.
- If facts are superseded, ingest the new fact and include citation/source so lifecycle can reconcile.
