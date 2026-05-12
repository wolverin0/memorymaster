# Detailed Security Findings

Audit method: STRIDE by component plus OWASP 2021 mapping. Findings below cite concrete code paths read during the audit. If no code path supported a threat, the matrix records a no-threat justification instead of inventing one.

## Findings

### F-001 - Dashboard POST endpoints allow unauthenticated state and process mutation

Severity: High  
STRIDE: Spoofing, Elevation of Privilege, Tampering  
OWASP: A01 Broken Access Control, A07 Identification and Authentication Failures, A04 Insecure Design  
Evidence: `memorymaster/dashboard.py:580`, `memorymaster/dashboard.py:1030`, `memorymaster/dashboard.py:1083`, `memorymaster/dashboard.py:475`

Attack scenario: A browser tab or local process that can reach the dashboard sends `POST /api/triage/action` with `{"claim_id": 1, "action": "approve_proposal"}` or `POST /api/operator/control` with `{"action": "start", "inbox_jsonl": "..."}`. `do_POST` routes the payload without authentication. `_handle_triage_action` can pin/unpin, mark reviewed, suppress, unsuppress, approve, or reject proposals. `_handle_operator_control` starts or stops the operator through `DashboardHTTPServer.start_operator`.

Expected outcome: unauthenticated user-controlled state changes or process lifecycle control.

Mitigation: require authentication and authorization for all dashboard POST routes; add CSRF protection and Origin checks for browser use; refuse non-loopback binding unless auth is configured.

### F-002 - Dashboard exposes events, operator stream, observability, and claim data without authentication

Severity: High  
STRIDE: Information Disclosure  
OWASP: A01 Broken Access Control, A09 Security Logging and Monitoring Failures  
Evidence: `memorymaster/dashboard.py:150`, `memorymaster/dashboard.py:742`, `memorymaster/dashboard.py:912`, `memorymaster/dashboard.py:1122`

Attack scenario: A client opens `/api/operator/stream?last=2000&follow=1`, `/api/events`, `/api/audit`, `/api/observability`, or `/api/claims`. The route map exposes these handlers and no auth gate runs before handler execution.

Expected outcome: claims, event payloads, operator activity, session/thread ids, tool counts, and operational metadata leak to any reachable client.

Mitigation: add dashboard authentication; separate read roles from operator-control roles; redact payloads and sensitive fields by default; disable operator stream unless explicitly enabled.

### F-003 - MCP mutating tools accept caller-controlled database and workspace targets

Severity: Medium  
STRIDE: Tampering, Elevation of Privilege  
OWASP: A01 Broken Access Control, A04 Insecure Design  
Evidence: `memorymaster/mcp_server.py:164`, `memorymaster/mcp_server.py:177`, `memorymaster/mcp_server.py:357`, `memorymaster/mcp_server.py:367`, `memorymaster/mcp_server.py:953`

Attack scenario: A tool caller passes `db` as an alternate SQLite path and invokes `init_db`, `ingest_claim`, `pin_claim`, `compact_memory`, or proposal-resolution tools. `_resolve_db` returns the non-default caller value and `_service` constructs `MemoryService` on that target.

Expected outcome: the MCP process writes, initializes, compacts, or mutates an arbitrary DB path accessible to the process.

Mitigation: enforce an allowlist of DB targets and workspace roots; remove per-call overrides from mutating tools; bind tools to the configured server workspace.

### F-004 - Cross-scope and raw-memory MCP tools lack sensitive/scope authorization

Severity: Medium  
STRIDE: Information Disclosure  
OWASP: A01 Broken Access Control  
Evidence: `memorymaster/mcp_server.py:986`, `memorymaster/mcp_server.py:1003`, `memorymaster/mcp_server.py:1214`

Attack scenario: A tool caller invokes `list_events`, `search_verbatim`, or `federated_query`. `federated_query` explicitly queries across all scopes; `search_verbatim` returns raw conversation memory; `list_events` serializes event rows. These paths do not apply the same `allow_sensitive` gate seen in `query_memory` and `list_claims`.

Expected outcome: cross-project memory, event payloads, or raw transcript fragments can be returned outside an intended project boundary.

Mitigation: require caller identity and scope allowlists for all read tools; add sensitive filtering to verbatim/event/federated responses; require explicit admin privilege for cross-scope federation.

### F-005 - Dashboard JSON body reads are unbounded

Severity: Medium  
STRIDE: Denial of Service  
OWASP: A05 Security Misconfiguration  
Evidence: `memorymaster/dashboard.py:606`

Attack scenario: A client sends a POST with a huge `Content-Length`. `_read_json_body` reads `length` bytes into memory before JSON parsing.

Expected outcome: memory pressure, slow parsing, and handler-thread exhaustion.

Mitigation: cap body size before reading, return `413 Payload Too Large`, and set socket/read timeouts.

### F-006 - Operator SSE stream can pin threads indefinitely

Severity: Medium  
STRIDE: Denial of Service  
OWASP: A05 Security Misconfiguration  
Evidence: `memorymaster/dashboard.py:1122`, `memorymaster/dashboard.py:1137`

Attack scenario: A client opens many `/api/operator/stream?follow=1` requests. `_handle_operator_stream` keeps the HTTP connection open and `_follow_stream` loops until client disconnect.

Expected outcome: each connection consumes a thread in `ThreadingHTTPServer`; many clients can starve the dashboard.

Mitigation: add max concurrent stream clients, authentication, idle timeouts, and a default `follow=0` mode for unauthenticated or read-only clients.

### F-007 - Dashboard reflects internal exception messages to clients

Severity: Medium  
STRIDE: Information Disclosure  
OWASP: A05 Security Misconfiguration, A09 Logging and Monitoring Failures  
Evidence: `memorymaster/dashboard.py:568`, `memorymaster/dashboard.py:580`

Attack scenario: A malformed request triggers a backend exception in a handler. `do_GET` and `do_POST` return `Internal server error: {exc}`.

Expected outcome: filesystem paths, SQL errors, DB names, or provider details may be exposed to a client.

Mitigation: return generic error responses and log detailed exceptions server-side with request ids.

### F-008 - Configurable LLM base URLs create SSRF and prompt-exfiltration risk

Severity: Medium  
STRIDE: Information Disclosure, Elevation of Privilege  
OWASP: A10 Server-Side Request Forgery, A05 Security Misconfiguration  
Evidence: `memorymaster/llm_provider.py:165`, `memorymaster/llm_provider.py:210`, `memorymaster/llm_provider.py:464`

Attack scenario: A hostile deployment environment sets `OPENAI_BASE_URL` or `OLLAMA_URL` to an internal service or attacker-controlled endpoint. `call_llm` posts prompt and claim text to the configured provider.

Expected outcome: prompt/claim data exfiltration or HTTP requests from the MemoryMaster process to unintended internal services.

Mitigation: validate provider URLs against allowed schemes/hosts; require explicit unsafe override for private IPs or non-default hosts; redact prompts before sending to non-default providers.

### F-009 - Mutations lack attributable actor identity

Severity: Medium  
STRIDE: Repudiation  
OWASP: A09 Security Logging and Monitoring Failures  
Evidence: `memorymaster/mcp_server.py:934`, `memorymaster/mcp_server.py:953`, `memorymaster/dashboard.py:1030`

Attack scenario: A caller redacts a claim, pins a claim, or marks triage state. MCP `redact_claim_payload` accepts a caller-supplied `actor`; `pin_claim` records no explicit actor in the tool path; dashboard triage records `{"source": "dashboard"}`.

Expected outcome: the audit trail can show what changed but not who made the change.

Mitigation: add authenticated principal identity to MCP/dashboard contexts and persist actor, route/tool name, request id, and origin.

### F-010 - LLM error logging includes URL prefixes that may contain credentials

Severity: Low  
STRIDE: Information Disclosure  
OWASP: A02 Cryptographic Failures  
Evidence: `memorymaster/llm_provider.py:93`, `memorymaster/llm_provider.py:319`, `memorymaster/llm_provider.py:344`

Attack scenario: A Google LLM request fails; `_call_google` places the API key in the URL query string and `_http_post` logs `url[:60]`. Current default URL length likely keeps the key outside the first 60 characters, but this depends on model and URL shape.

Expected outcome: partial or future full key leakage into logs if URL structure changes.

Mitigation: never log URL strings containing credentials; log provider, host, and model separately after stripping query strings.

### F-011 - Caller-controlled provenance can poison memory history

Severity: Low  
STRIDE: Tampering  
OWASP: A03 Injection, A08 Software and Data Integrity Failures  
Evidence: `memorymaster/mcp_server.py:185`, `memorymaster/mcp_server.py:367`, `memorymaster/service.py:161`

Attack scenario: A tool caller supplies fake `sources_json` and `source_agent`. The MCP tool parses these into citations and passes `source_agent` to service ingest.

Expected outcome: stored claims can appear to originate from a misleading source or agent, reducing audit reliability.

Mitigation: derive source agent from authenticated transport identity; validate citation source schemes; mark user-supplied provenance as untrusted.

### F-012 - KeyRotator sleeps in the request path when all keys are on cooldown

Severity: Low  
STRIDE: Denial of Service  
OWASP: A05 Security Misconfiguration  
Evidence: `memorymaster/key_rotator.py:62`, `memorymaster/key_rotator.py:75`

Attack scenario: All Gemini keys hit rate limits. `next_key` computes a wait and sleeps before returning a key.

Expected outcome: steward/LLM request paths block during cooldown, reducing throughput and potentially stacking workers.

Mitigation: return retry metadata to callers instead of sleeping, or move waits to a scheduler queue.

### F-013 - Non-loopback dashboard bind is possible without auth requirement

Severity: Low  
STRIDE: Spoofing, Information Disclosure  
OWASP: A05 Security Misconfiguration  
Evidence: `memorymaster/dashboard.py:1150`, `memorymaster/dashboard.py:1160`

Attack scenario: Operator starts dashboard with `--host 0.0.0.0`. The same unauthenticated routes are then reachable over the network.

Expected outcome: remote clients can read and mutate memory state if network ACLs permit.

Mitigation: refuse non-loopback host unless an auth secret is configured; print a prominent warning and require `--unsafe-no-auth-network-bind`.

### F-014 - Mobile review queue SQL is parameterized; no injection path found

Severity: Info  
STRIDE: Tampering  
OWASP: A03 Injection  
Evidence: `memorymaster/dashboard.py:814`, `memorymaster/dashboard.py:828`

No applicable threat found: `/api/v1/review-queue` builds SQL from fixed clause fragments and passes user values as SQLite parameters. The dynamic `where_sql` does not include raw user strings.

Keep using fixed clause fragments and bound parameters.

### F-015 - Ingest sensitivity filter is present

Severity: Info  
STRIDE: Information Disclosure  
OWASP: A02 Cryptographic Failures  
Evidence: `memorymaster/mcp_server.py:136`, `memorymaster/mcp_server.py:367`, `memorymaster/service.py:202`, `memorymaster/service.py:304`

No direct plaintext-secret ingest path found in scoped ingest flow: MCP rejects sensitive `text` before service ingest, and service ingest runs `sanitize_claim_input` over text, object value, and citations. Sensitive claims record policy events and optional encrypted payload metadata.

Residual risk: provenance metadata remains caller-controlled and encryption depends on configuration.

## STRIDE by Component Matrix

| Component | Spoofing | Tampering | Repudiation | Information Disclosure | DoS | Elevation of Privilege |
|---|---|---|---|---|---|---|
| MCP server | Caller identity is implicit in MCP client trust; finding F-003. | DB/workspace override and mutating tools; F-003, F-011. | Actor not consistently recorded; F-009. | Raw/federated/event reads; F-004. | Expensive `run_cycle`, `run_steward`, and unbounded metadata fields are residual risks; no standalone high-confidence DoS beyond dashboard/rotator. | Mutating tools can operate with process authority; F-003. |
| Dashboard | No auth/session; F-001, F-013. | POST mutations; F-001. | Generic dashboard source; F-009. | GET APIs/SSE; F-002, F-007. | Unbounded body and SSE threads; F-005, F-006. | Operator start/stop and proposal approval; F-001. |
| Service ingest | No direct user principal; upstream issue. | Provenance poisoning; F-011. | Source-agent caller controlled; F-009/F-011. | Sensitivity controls present; F-015. | Entity extraction exceptions are swallowed; no applicable DoS path found in scoped ingest code. | No direct EoP in service ingest without upstream tool/dashboard access. |
| LLM provider | Provider identity is env config; no user auth surface. | Fallback model env is temporarily swapped but restored; no tamper path without env control. | Provider calls logged without request principal; low residual. | URL logging and prompt exfil to configured provider; F-008, F-010. | KeyRotator sleep; F-012. | SSRF via provider base URL can reach internal services; F-008. |

STRIDE coverage: 6/6 categories, 100%.

## No Applicable Threat Notes

Vulnerable components (A06): dependency inventory and SCA were outside the requested source-file scope. No vulnerable component use was proven from the scoped code alone.

Jinja rendering: no Jinja renderer was found in `memorymaster/dashboard.py`; the scoped dashboard uses inline strings and `escape`.

SQL injection: no SQL injection path was found in the mobile review queue because values are parameterized. Other storage methods were not audited line-by-line beyond the requested components.
