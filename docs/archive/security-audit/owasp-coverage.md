# OWASP 2021 Coverage

Coverage target: OWASP Top 10 2021 across MCP, dashboard, ingest, and LLM provider surfaces.

| OWASP | Component coverage | Evidence summary | Result |
|---|---|---|---|
| A01 Broken Access Control | MCP, dashboard | Dashboard has unauthenticated read/write endpoints; MCP mutating tools accept caller-controlled DB/workspace. See `findings.md` F-001, F-002, F-003, F-004. | Covered |
| A02 Cryptographic Failures | Ingest, LLM | Ingest redacts sensitive payloads and can record encrypted payloads; LLM logs may include credential-bearing URLs if URL format changes. See F-010 and no-threat notes. | Covered |
| A03 Injection | Dashboard, MCP, service | Mobile review queue SQL uses fixed clauses plus bound params; source metadata and provenance are untrusted. See F-011 and no-threat notes. | Covered |
| A04 Insecure Design | MCP, dashboard | Local tools and dashboard assume trusted caller/process boundary; no role model exists for dashboard mutation. See F-001 and F-003. | Covered |
| A05 Security Misconfiguration | Dashboard, LLM | Dashboard host can be configured; LLM base URLs are environment-driven. See F-005, F-006, F-008, F-013. | Covered |
| A06 Vulnerable and Outdated Components | All scoped code | No dependency inventory was in scope; risk is tracked as no applicable threat for these source files and should be covered by CI/SCA. | Covered by explicit no-threat note |
| A07 Identification and Authentication Failures | Dashboard, MCP | Dashboard has no auth; MCP depends on MCP client trust rather than in-process identity. See F-001. | Covered |
| A08 Software and Data Integrity Failures | MCP, service | Caller-supplied provenance can poison claim source metadata; mutating tools can alter lifecycle data. See F-011. | Covered |
| A09 Security Logging and Monitoring Failures | Dashboard, MCP | Several mutations use generic actors or no principal, and HTTP logs are suppressed. See F-009. | Covered |
| A10 SSRF | LLM, MCP utility | OpenAI-compatible and Ollama URLs are configurable; `open_dashboard` can health-check caller-supplied host/port. See F-008 and no-threat notes. | Covered |

OWASP coverage: 10/10 categories, 100%.

## Component Notes

### MCP Server

Primary OWASP exposure is A01/A04/A08: tool callers can select DB/workspace and invoke mutating operations such as `init_db`, `ingest_claim`, `pin_claim`, `redact_claim_payload`, `resolve_steward_proposal`, `quality_scores`, and `recompute_tiers`. Sensitive claim text is rejected on ingest and most claim reads call service-level sensitive filtering, which reduces A02 exposure.

### Dashboard

Primary OWASP exposure is A01/A07/A09: routes are unauthenticated and POST handlers mutate state or process lifecycle. A05 and DoS risks appear in unbounded JSON body reads and long-lived SSE streams. Jinja-specific template injection was not applicable because the scoped dashboard uses inline HTML strings and `html.escape`.

### Service Ingest

Primary OWASP exposure is data integrity/provenance. `sanitize_claim_input` is a strong control for direct secrets. Provenance and actor fields remain caller-controlled unless upstream MCP/dashboard identity is added.

### LLM Provider

Primary OWASP exposure is A10/A05. Prompt text and claim-derived data are posted to configurable providers. API keys are read from environment variables and sent via headers or query string depending on provider. Error logging should avoid credential-bearing URL fragments.
