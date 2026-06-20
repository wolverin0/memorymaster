# Security Audit Threat Model

Date: 2026-05-11  
Repository: `wolverin0/memorymaster`  
Scope: MCP tools, dashboard HTTP routes, ingest path, and LLM provider key handling.

## Source Enumeration

Files read:

| File | Status |
|---|---|
| `memorymaster/mcp_server.py` | Read. FastMCP tools and helpers enumerated. |
| `memorymaster/dashboard.py` | Read. `BaseHTTPRequestHandler` routes and handlers enumerated. |
| `memorymaster/dashboard_helpers.py` | Not found on `main` via GitHub contents API. |
| `memorymaster/dashboard_helpers_extra.py` | Not found on `main` via GitHub contents API. |
| `memorymaster/service.py` | Read. Ingest, query, list, and mutation paths inspected. |
| `memorymaster/llm_provider.py` | Read. Provider URLs, API key use, fallback, and HTTP error logging inspected. |
| `memorymaster/key_rotator.py` | Read for `KeyRotator`, because `llm_provider.py` imports it for Gemini file rotation. |

## MCP Tool Entrypoints

`memorymaster/mcp_server.py` creates `FastMCP("memorymaster")` and registers these tools:

| Tool | Line | Trust notes |
|---|---:|---|
| `init_db` | 357 | Mutating; caller controls `db` and `workspace`. |
| `ingest_claim` | 367 | Mutating; validates text length and rejects sensitive text before service ingest. |
| `run_cycle` | 478 | Mutating/expensive; caller controls policy knobs. |
| `run_steward` | 499 | Mutating/expensive; has `allow_sensitive` gate but broad operational reach. |
| `classify_query` | 561 | Read/compute only. |
| `query_memory` | 592 | Read; has `allow_sensitive` gate and default scope allowlist. |
| `query_for_context` | 692 | Read; has `allow_sensitive` gate and default scope allowlist. |
| `query_for_task` | 779 | Read; derives project scope from argument/env/workspace. |
| `read_active_tasks` | 839 | Filesystem read under caller-supplied project root. |
| `list_claims` | 912 | Read; service-level sensitive filter unless bypass enabled. |
| `redact_claim_payload` | 934 | Mutating; no explicit actor authentication beyond caller-supplied `actor`. |
| `pin_claim` | 953 | Mutating; no explicit actor authentication. |
| `compact_memory` | 974 | Mutating/retention; caller controls retention days. |
| `list_events` | 986 | Read; no sensitive gate observed. |
| `search_verbatim` | 1003 | Read raw transcript memory; no sensitive gate observed. |
| `open_dashboard` | 1020 | Network reachability check to caller-supplied host/port. |
| `list_steward_proposals` | 1075 | Read; returns proposal workflow data. |
| `resolve_steward_proposal` | 1093 | Mutating; can apply approved proposal. |
| `extract_entities` | 1115 | Mutating entity graph; caller controls text unless claim text loaded. |
| `entity_stats` | 1136 | Read; entity graph stats. |
| `find_related_claims` | 1145 | Read graph traversal by caller-supplied entity names. |
| `quality_scores` | 1163 | Mutating/recompute; feedback tables. |
| `recompute_tiers` | 1174 | Mutating/recompute. |
| `query_meta_decisions` | 1179 | Cross-scope decision aggregation. |
| `federated_query` | 1214 | Cross-scope query across all claims. |

Shared MCP helpers:

| Helper | Line | Notes |
|---|---:|---|
| `_validate_tool_input` | 96 | Pydantic validation; caps only `text`, `body`, `content` at 10,000 chars. |
| `_sensitive_input_error` | 136 | Rejects direct sensitive claim text before ingest. |
| `_resolve_db` | 164 | Returns caller-provided non-default DB path. |
| `_resolve_workspace` | 170 | Returns caller-provided workspace path. |
| `_service` | 177 | Constructs `MemoryService` from resolved DB/workspace. |
| `_parse_sources_json` | 185 | Parses citation metadata from caller-supplied JSON. |

## Dashboard Routes

`memorymaster/dashboard.py` uses `ThreadingHTTPServer` and `BaseHTTPRequestHandler`, not Flask/FastAPI route decorators.

GET route map:

| Route | Handler | Line |
|---|---|---:|
| `/health` | inline JSON | 150 |
| `/` | `_write_dashboard` | 151 |
| `/dashboard` | `_write_dashboard` | 152 |
| `/api/claims` | `_handle_claims` | 153 |
| `/api/events` | `_handle_events` | 154 |
| `/api/timeline` | `_handle_timeline` | 155 |
| `/api/conflicts` | `_handle_conflicts` | 156 |
| `/api/review-queue` | `_handle_review_queue` | 157 |
| `/api/v1/review-queue` | `_handle_mobile_review_queue` | 158 |
| `/api/action-proposals` | `_handle_action_proposals` | 159 |
| `/api/atlas/version` | `_handle_atlas_version` | 160 |
| `/api/retrieval` | `_handle_retrieval` | 161 |
| `/api/audit` | `_handle_audit` | 162 |
| `/api/namespaces` | `_handle_namespaces` | 163 |
| `/api/session-stats` | `_handle_session_stats` | 164 |
| `/api/observability` | `_handle_observability` | 165 |
| `/metrics/validation-latency` | `_handle_validation_latency` | 166 |
| `/api/operator/status` | inline JSON | 167 |
| `/api/operator/stream` | `_handle_operator_stream` | 168 |

POST route map:

| Route | Handler | Line |
|---|---|---:|
| `/api/triage/action` | `_handle_triage_action` | 580 |
| `/api/operator/control` | `_handle_operator_control` | 580 |
| `/api/action-proposals/status` | `_handle_action_proposal_status` | 580 |

Additional dynamic route:

| Route pattern | Handler | Line |
|---|---|---:|
| `/claim/<id>/lineage` | `_handle_claim_lineage` | 172 |

Rendering:

The dashboard is generated with inline HTML strings and `html.escape` in lineage rendering. No Jinja import or template loader was found in the scoped dashboard file.

## Ingest Paths

MCP ingest path:

1. `ingest_claim` validates pydantic input and caps `text`.
2. `_sensitive_input_error` rejects direct secrets in `text`.
3. `_parse_sources_json` parses citation metadata.
4. `_service` constructs `MemoryService` with caller-provided DB/workspace.
5. `MemoryService.ingest` sanitizes text, object value, and citations, writes the claim, records policy events for redaction/encryption, syncs Qdrant, and fires webhook.

Service ingest path:

| Step | File:line | Security note |
|---|---|---|
| Empty text rejected, missing citations defaulted | `memorymaster/service.py:161` | Default citation may obscure provenance. |
| Idempotency/content hash dedupe | `memorymaster/service.py:180` | Uses text/scope/tenant hash. |
| `sanitize_claim_input` | `memorymaster/service.py:202` | Redacts sensitive text/object/citation data. |
| Entity extraction best effort | `memorymaster/service.py:212` | Exceptions swallowed. |
| `store.create_claim` | `memorymaster/service.py:260` | Persist sanitized claim. |
| Sensitive policy events | `memorymaster/service.py:304` | Records redaction/encryption metadata. |
| Webhook fire | `memorymaster/service.py:326` | Outbound integration path. |

## Trust Boundaries

### Client -> MCP

Caller is an LLM/tool client. It can supply DB path, workspace path, scopes, source metadata, retention settings, proposal ids, and query modes. Boundary controls are pydantic validation, the 10k text cap, sensitive text rejection on ingest, and service-level sensitive filtering on most claim reads. Missing controls are identity, per-tool authorization, and a DB/workspace allowlist.

### Browser -> Dashboard

Caller is any browser or HTTP client that can reach the bind host. There is no authentication, CSRF token, session, or origin validation in `do_GET`/`do_POST`. Boundary controls are loopback default bind, per-parameter parsing, and hard min/max limits for some query parameters.

### Dashboard -> DB

Dashboard handlers call `MemoryService` and direct store connections. Parameterized SQL is used in mobile review queue. Dashboard bypasses caller identity and performs mutations as local process authority.

### MCP -> DB

MCP tools construct `MemoryService` and entity/feedback helpers with `_resolve_db(db)`. The boundary depends on the process filesystem permissions. There is no MCP-level DB allowlist or tenant identity by default.

### LLM Provider -> LLM

`call_llm` sends prompts and text to configured providers. API keys come from environment variables or Gemini rotators. Provider base URLs are configurable for OpenAI-compatible and Ollama flows, so deployment config becomes the SSRF/exfiltration control point.
