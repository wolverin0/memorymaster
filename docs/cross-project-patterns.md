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

## Default Gates

The normal service query path builds the candidate statuses from `confirmed`, optionally `stale`, optionally `conflicted`, and optionally `candidate` (`memorymaster/service.py:446-455`). The MCP `query_memory` wrapper enables `include_stale=True`, `include_conflicted=True`, and `include_candidates=True` by default (`memorymaster/mcp_server.py:593-606`). `federated_query` also includes candidates and inherits the `query_rows` defaults for stale and conflicted claims (`memorymaster/service.py:1278-1283`, `memorymaster/service.py:496-510`).

Archived claims are excluded from the normal query path because both legacy and hybrid paths call storage with `include_archived=False` (`memorymaster/service.py:459-467`, `memorymaster/service.py:536-543`). Storage also adds `status <> 'archived'` whenever `include_archived` is false (`memorymaster/_storage_read.py:170-171`, `memorymaster/postgres_store.py:536-537`).

Sensitive payloads are excluded by default in the normal query path. `query_rows` resolves `allow_sensitive` with `deny_mode="filter"` (`memorymaster/service.py:520-524`), then filters claims through `is_sensitive_claim` when sensitive access is not allowed (`memorymaster/service.py:544-545`). `is_sensitive_claim` detects redacted markers and sensitive-pattern findings in `text`, `object_value`, `subject`, and `predicate` (`memorymaster/security.py:356-363`).

Important current-behavior caveat: these MCP query tools do not categorically gate a claim solely because `visibility == "sensitive"`. The service's normal sensitive filter is content-based (`memorymaster/security.py:356-363`). The only visibility filter in the legacy query path runs when `requesting_agent` is supplied (`memorymaster/service.py:470-472`), and the `query_memory` MCP wrapper does not pass `requesting_agent` into `query_rows` (`memorymaster/mcp_server.py:643-653`).

## Per-Tool Contract

### `query_memory`

`query_memory` is the default project recall tool. Its MCP signature includes `workspace`, `limit`, `retrieval_mode`, `include_stale`, `include_conflicted`, `include_candidates`, `allow_sensitive`, `scope_allowlist`, and `detail_level` (`memorymaster/mcp_server.py:592-606`).

Default scope filter:

- Blank `scope_allowlist` expands to the derived current project scope plus `global` (`memorymaster/mcp_server.py:274-289`).
- A non-blank `scope_allowlist` replaces the default list rather than adding to it (`memorymaster/mcp_server.py:274-277`).
- The service normalizes and deduplicates the list (`memorymaster/service.py:400-414`), then storage enforces exact scope membership (`memorymaster/_storage_read.py:173-178`, `memorymaster/postgres_store.py:544-549`).

Included by default on the normal service path:

- `confirmed`, `stale`, `conflicted`, and `candidate` claims because the MCP wrapper defaults all include flags to true (`memorymaster/mcp_server.py:593-606`) and `query_rows` builds the status list accordingly (`memorymaster/service.py:446-455`, `memorymaster/service.py:526-527`).
- Claims in the derived project scope and `global` when no explicit `scope_allowlist` is supplied (`memorymaster/mcp_server.py:274-289`).
- Full claim dictionaries at `detail_level="standard"`; `summary` truncates to selected fields and `full` re-fetches citations (`memorymaster/mcp_server.py:560-590`, `memorymaster/mcp_server.py:654-678`).

Excluded by default on the normal service path:

- Archived claims (`memorymaster/service.py:459-467`, `memorymaster/service.py:536-543`).
- Claims in other project scopes, `user`, and `team:<name>` unless explicitly allowlisted (`memorymaster/mcp_server.py:274-289`, `memorymaster/_storage_read.py:173-178`).
- Content-sensitive claims unless `allow_sensitive` is true and the security override allows it (`memorymaster/service.py:520-545`, `memorymaster/security.py:193-210`).

How to widen:

- Use `scope_allowlist="project:pather,user,global"` to include exactly those scopes.
- Use `scope_allowlist="project:pather,project:wezbridge,global"` for a bounded multi-project query.
- Use `federated_query` when the intended behavior is all-scope search.

### `query_meta_decisions`

`query_meta_decisions` aggregates decision and architecture claims across project scopes. Its MCP signature accepts `query`, `claim_types`, `top_n`, `db`, and `workspace` (`memorymaster/mcp_server.py:1182-1199`), then calls `MemoryService.query_meta_decisions` (`memorymaster/mcp_server.py:1206-1212`).

Default scope filter:

- There is no caller-supplied scope filter.
- With a non-empty query string, the service calls `query_rows(..., scope_allowlist=None)` (`memorymaster/service.py:735-743`).
- With an empty query string, the service calls `store.list_claims` without `scope_allowlist` (`memorymaster/service.py:746-752`).
- After retrieval, it keeps only scopes that start with `project:` (`memorymaster/service.py:804-810`).

Included by default:

- `confirmed`, `stale`, `conflicted`, and `candidate` claims (`memorymaster/service.py:730-734`).
- Only `claim_type` values in the requested `claim_types` set; defaults are `decision` and `architecture` (`memorymaster/mcp_server.py:1183-1186`, `memorymaster/service.py:723-727`, `memorymaster/service.py:811-813`).
- Only `project:<slug>` scopes after post-filtering (`memorymaster/service.py:804-810`).

Excluded by default:

- `user`, `team:<name>`, and `global` scopes, because the post-filter rejects scopes that do not start with `project:` (`memorymaster/service.py:804-810`).
- Sensitive claims, because the aggregation loop skips `is_sensitive_claim(claim)` (`memorymaster/service.py:804-807`).
- Archived claims, because both the query path and empty-query path use `include_archived=False` (`memorymaster/service.py:736-752`).
- Non-decision and non-architecture claim types unless the caller expands `claim_types` (`memorymaster/mcp_server.py:1183-1186`, `memorymaster/service.py:811-813`).

How to widen:

- Use `claim_types=["decision","architecture","constraint"]` to include additional claim types.
- There is currently no MCP parameter to include `user`, `team:<name>`, or `global` in `query_meta_decisions`; use `federated_query` or explicit `query_memory(scope_allowlist=...)` for those scopes.
