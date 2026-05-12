# Cross-Project Federation Contract

This document defines the current federation contract for the `query_memory`, `query_meta_decisions`, and `federated_query` MCP tools. It is behavior-derived: every contract point below cites the implementation or lifecycle rule that currently enforces it.

## Source Map

- Scope vocabulary is defined in `.claude/rules/claims-lifecycle.md:32-40`.
- MCP workspace scope derivation and default query scope expansion live in `memorymaster/mcp_server.py:237-289`.
- `query_memory` is exposed in `memorymaster/mcp_server.py:592-606` and calls `MemoryService.query_rows` at `memorymaster/mcp_server.py:643-653`.
- `query_meta_decisions` is exposed in `memorymaster/mcp_server.py:1182-1212` and calls `MemoryService.query_meta_decisions`.
- `federated_query` is exposed in `memorymaster/mcp_server.py:1214-1248` and calls `MemoryService.federated_query`.
- SQLite query filtering is implemented in `memorymaster/_storage_read.py:146-180`; PostgreSQL query filtering mirrors it in `memorymaster/postgres_store.py:509-560`.

The requested wiki source directory, `obsidian-vault/wiki/project-memorymaster/`, is not present in this worktree, so this contract uses the code and lifecycle rule sources above.

## Scopes Overview

Canonical scope forms are:

| Scope | Meaning | Default `query_memory` visibility |
|---|---|---|
| `project:<slug>` | Per-project memory. This is the normal scope for project facts. | Included only when it matches the derived workspace project scope, or when explicitly listed in `scope_allowlist`. |
| `user` | User-level memory such as workstyle, tool preferences, and cross-project preferences. | Not included by default. Explicitly include it with `scope_allowlist=user` or use an unscoped federation tool where applicable. |
| `team:<name>` | Team-shared memory. | Not included by default. Explicitly include it with `scope_allowlist=team:<name>` or use `federated_query`. |
| `global` | System-wide facts. | Included by default with the current project scope. |

The scope vocabulary above comes from `.claude/rules/claims-lifecycle.md:32-40`. A blank or `project` ingest scope is resolved to the current workspace-derived `project:<slug>` by `_effective_ingest_scope` in `memorymaster/mcp_server.py:267-271`. The workspace project slug is derived from the resolved workspace directory name by `_project_scope` in `memorymaster/mcp_server.py:237-264`.

For `query_memory`, default scope precedence is:

1. If `scope_allowlist` is supplied, it is parsed as a comma-separated list and used as-is (`memorymaster/mcp_server.py:208-210`, `memorymaster/mcp_server.py:274-277`).
2. If `scope_allowlist` is blank, the effective allowlist is `[current project scope, global]` (`memorymaster/mcp_server.py:274-289`).
3. Storage applies the allowlist as `scope IN (...)` (`memorymaster/_storage_read.py:173-178`, `memorymaster/postgres_store.py:544-549`).

`global` is therefore ambient project-visible memory for `query_memory`; `user` is personal cross-project memory but is not ambiently visible unless the caller explicitly asks for it. `query_meta_decisions` is narrower than both: after retrieval it keeps only scopes beginning with `project:` (`memorymaster/service.py:804-810`). `federated_query` is broader: it passes `scope_allowlist=None`, which means no scope filter (`memorymaster/service.py:1271-1283`).
