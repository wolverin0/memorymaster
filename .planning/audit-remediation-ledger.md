# Audit Remediation Ledger

**Baseline commit:** `9c2e2bf4b10c9acbbb5ed5832af730b3b3ca851a`
**Roadmap:** `.planning/REMEDIATION-OPTIMIZATION-PLAN-2026-07-10.md`
**Baseline audit:** `.planning/audits/2026-07-10-baseline/audit-report.md`

Statuses: `OPEN`, `IN-PROGRESS`, `RESOLVED`, `BLOCKED-EXTERNAL`, `BLOCKED-POLICY`.

> WARNING LEGAL REVIEW REQUIRED — applies to risk acceptance and
> `BLOCKED-POLICY` dispositions only. Do not mark `ACCEPT` without legal sign-off;
> this warning does not pause remediation.

| ID | Primary domain | Severity / exploitability | Summary | Package | Status | Acceptance evidence |
|---|---|---|---|---|---|---|
| MM-SEC-01 | Security/Database | Critical / EXPLOITABLE-NOW / H1 | MCP roles, project scopes, and Postgres tenant isolation are not enforced centrally | R1.1-R1.2 | BLOCKED-EXTERNAL | Owner: Codex remediation branch. R1.1 complete. PostgreSQL application connections are team-only and bind tenant, principal, and immutable scopes transaction-locally; schema work requires a distinct verified migrator. v0011 FORCE RLS defines restricted role/table/policy and append-only event contracts, a tenant-derived hash-only event-head function, and deny-only governance/raw tables. v0012 defines six partial unique indexes: public keys are tenant + exact-scope local; non-public keys additionally include exact visibility/principal; ambiguity without exact scope fails closed; every team claim requires a nonblank owner. Startup validates literal-sensitive policy/index/function fingerprints, exact event and claims trigger inventories, required event SELECT/INSERT plus forbidden table/column UPDATE and DELETE, a privileged event-head owner, and the strict validated owner constraint. Supersession rejects self/cross-tenant/scope/visibility/owner references and the canonical path atomically commits reciprocal pointers plus one event; v0012 preflights unsafe legacy edges. Unsupported Postgres source/evidence/action/retry, read-only, merge, delta, and tenant-bound CLI surfaces now fail before driver or filesystem access; whitespace-wrapped DSNs cannot bypass routing. The parity harness requires two distinct roles plus disposable opt-in, uses UUID tenant namespaces, and performs no destructive cleanup. Closure matrix: 349 passed, 47 externally gated skips; direct surface/factory matrix: 57 passed. Full isolated non-ML gate: 3,552 passed, 69 skipped, 95 deselected, 22 intentional xfails, 2 warnings in 858.47s. Commit evidence is the commit containing this row. Rollback: keep the team profile disabled and revert this package; schema rollback requires a verified backup/forward repair. Repository work for R1.1-R1.2 is complete. Real two-role PostgreSQL evidence plus approved brownfield owner/duplicate/supersession-edge inventory/repair remain `BLOCKED-EXTERNAL`; the Team/Postgres profile remains blocked. |
| MM-SEC-02 | Security/Database | Critical / EXPLOITABLE-NOW / H1 | Qdrant can return archived, sensitive, cross-scope/tenant, or orphan payloads | R1.3,R2.1 | IN-PROGRESS | Owner: Codex remediation branch. R1.3 containment is repository-complete: local-trusted claim requests and auto-classified Qdrant recommendations fall back to authoritative lexical retrieval with requested/classified/effective metadata; team semantic requests are denied before tool dispatch; prompt-context fallback is disconnected; verbatim vector/hybrid requests use FTS5; CLI denial occurs before service/backend construction; and every direct claim/verbatim/fallback read adapter raises before model, network, or raw payload access. Qdrant upsert/sync/reconcile/count-ID maintenance remains available. Adversarial containment: 18 passed; Qdrant/verbatim/classifier matrix: 153 passed; CLI/setup/MCP regression matrix: 182 passed; explicit ML gate: 14 passed. Full isolated non-ML gate: 3,572 passed, 69 skipped, 95 deselected, 20 intentional xfails, 2 warnings in 882.06s; collection: 3,756; Ruff: clean. Independent blocker-only review found no first-party raw payload-search path. Commit evidence is the commit containing this row. Rollback: keep semantic retrieval disabled and revert this package. R2.1 governed ID-candidate rehydration remains unimplemented, and final authenticated/TLS Qdrant parity is `BLOCKED-EXTERNAL`; do not enable the semantic profile. |
| MM-OPS-01 | DevOps | Critical / EXPLOITABLE-LOW-EFFORT / H4 | Postgres Compose publishes a fixed credential | R1.5 | BLOCKED-EXTERNAL | Repository defaults are fail-closed and private (`a858419`); Compose rejects missing inputs and renders with synthetic required inputs. Historical credential rotation/recreation and a real network probe require operator action. |
| MM-SEC-03 | Security | High / EXPLOITABLE-NOW | Persisted metadata/provenance fields bypass sensitivity scanning | R1.4 | BLOCKED-EXTERNAL | Repository gateways and the aggregate-only inventory are complete (`a3e3824`, `702b59d`, `8d80abb`); focused final gate: 46 passed, 1 skipped. The authorized live read-only inventory found legacy sensitive/unscannable material, while Qdrant was unavailable; cleanup/redaction and product-data mutation remain forbidden without approval. |
| MM-SEC-04 | Security | High / EXPLOITABLE-NOW | Steward, compact-summary, verbatim, and integration writes bypass one gateway | R1.4 | RESOLVED | Durable auxiliary writers now use the shared sensitivity envelope and adversarial persistence matrices; the final observed compatibility failures were corrected and the exact 10-test failure set passed. Roll back by reverting the R1.4 commits while keeping affected ingestion disabled. |
| MM-ARCH-01 | Architecture/Database | High / BAD-PRACTICE | Entity registry and graph own incompatible `entities` schemas | R2.3 | OPEN | Normal init-to-graph MCP integration and FK check |
| MM-ARCH-02 | Architecture/Integrity | High / BAD-PRACTICE | MCP/hooks/CLI/Qdrant use contradictory retrieval planners and trust defaults | R2.1 | OPEN | Cross-surface ID-set and conversational-query parity |
| MM-LIFE-01 | Database/Reliability | High / BAD-PRACTICE | Scheduled archival uses raw SQL and bypasses lifecycle/vector evidence | R2.2 | OPEN | Version/event/timestamp/outbox assertions through scheduled path |
| MM-REL-02 | Reliability/Performance | Medium / BAD-PRACTICE | MCP reads take write locks and context detail modes retrieve twice | R2.2 | OPEN | Zero query write lock; one aggregated telemetry envelope |
| MM-OPS-02 | DevOps/Demo | High / BAD-PRACTICE | Docker/Helm publish HTTP on a stdio MCP process and use an invalid healthcheck | R3.4 | OPEN | Built-container readiness and MCP handshake |
| MM-OPS-03 | DevOps/Maintainability | High / BAD-PRACTICE | A release tag can publish without a blocking verified test artifact | R4.4 | OPEN | Deliberately failing release candidate cannot publish |
| MM-OPS-04 | Security/DevOps | High / EXPLOITABLE-LOW-EFFORT | Qdrant/Ollama ports are broadly exposed; images are mutable | R1.5,R3.4 | IN-PROGRESS | Phase 1 defaults are repository-complete (`a858419`, `9b3e16c`, `b71e18f`): loopback-only ports, required immutable digests, authenticated TLS Qdrant, redirect-safe credentials, and fail-closed supply-chain policy. Real image scans/runtime health remain `BLOCKED-EXTERNAL`; R3.4 service-entrypoint work remains backlog. |
| MM-PERF-01 | Performance/Cost | High / BAD-PRACTICE | Hybrid reads recompute and rewrite candidate embeddings | R3.1 | OPEN | Warm query: one query embed, zero candidate embeds/writes |
| MM-PERF-02 | Performance | Medium / BAD-PRACTICE | Each process cold-scans corpus token statistics and caches forever | R3.2 | OPEN | Generation-aware/token-specific stats benchmark |
| MM-PERF-03 | Performance/Cost | High / EXPLOITABLE-NOW | Qdrant reconciliation repeats/truncates tens of thousands of embeddings | R3.1 | OPEN | Paginated incremental convergence benchmark |
| MM-PERF-04 | Database/Performance | Medium / BAD-PRACTICE | Million-row event queries lack event-type composite indexes | R3.2 | OPEN | Versioned parity migration plus EXPLAIN/timing evidence |
| MM-UX-01 | UX/Maintainability | High / BAD-PRACTICE | Setup reports success without verifying the requested memory loop | R3.3 | OPEN | Component-level profile verification and nonzero failures |
| MM-UX-02 | UX/Reliability | High / BAD-PRACTICE | Dashboard removes review rows before mutation success | R4.3 | OPEN | Browser failure/retry evidence; row remains on rejected POST |
| MM-UX-03 | UX | Medium / BAD-PRACTICE | Dashboard hides failures and has labeling, contrast, responsive, and evidence-discovery gaps | R4.3 | OPEN | Browser/a11y/mobile acceptance suite |
| MM-PRIV-01 | Compliance | High / BAD-PRACTICE | Automatic transcript capture/remote processing lacks an explicit consent boundary | R2.4,R3.5 | OPEN | Quiet default, explicit processor/capture choices |
| MM-PRIV-02 | Compliance | High / BAD-PRACTICE | No complete export, erasure, or retention workflow across copies | R3.5 | OPEN | Privacy export/erase dry-run manifest across all stores |
| MM-COST-01 | Cost | High / EXPLOITABLE-NOW | Default Stop-hook LLM calls have no finite persisted budget | R2.4 | OPEN | Restart-safe global/provider/session caps |
| MM-COST-02 | Cost | High / EXPLOITABLE-NOW | Verbatim capture reprocesses sessions and retains data indefinitely | R2.4 | OPEN | Incremental cursor plus frozen retention envelope |
| MM-COST-03 | Cost/Reliability | Medium / BAD-PRACTICE | Cost and intake controls are fragmented/process-local | R2.4,R3.2 | OPEN | Atomic durable ledger and multi-process tests |
| MM-DEMO-01 | Demo/Integrity | High / EXPLOITABLE-NOW | Atlas defaults persist fabricated mock evidence at high confidence | R2.5 | OPEN | Default command rejects; mock rows cannot feed claims/actions |
| MM-MAINT-01 | Maintainability/Integrity | Medium / BAD-PRACTICE | Versions, tool counts, roadmaps, and install claims drift | R4.4 | OPEN | Generated single-source values checked by CI |
| MM-MAINT-02 | Architecture/Maintainability | Medium / BAD-PRACTICE | Oversized facades, hardwired extensions, dead plugin seam, and lingering shims | R4.1-R4.2 | OPEN | Measured boundary/size budgets and supported shim policy |
| MM-REL-03 | Reliability | Medium / BAD-PRACTICE | Media retry rows have no expired-lease recovery | R3.5 | OPEN | Worker-death lease reclaim test |
| MM-OPS-05 | Missing Operations | High / BAD-PRACTICE | Recovery defaults stop at same-machine SQLite snapshots | R3.5 | BLOCKED-EXTERNAL | Off-device SQLite/Postgres restore drill and RPO/RTO |
| MM-OBS-01 | Missing Operations | High / BAD-PRACTICE | Failures/metrics are process-local without central alert ownership | R3.5 | OPEN | Persistent metrics, trace/error capture, alert tests/runbook |
| MM-DB-01 | Database | Medium / BAD-PRACTICE | Fast schema fingerprint can omit legacy ensure-helper changes | R3.2 | OPEN | All DDL versioned or fingerprint covers every schema source |
| MM-INTEGRITY-01 | Code Integrity | Medium / BAD-PRACTICE | `importlib.util` probe is wrong in clean Python and its test masks failure | R4.4 | OPEN | Clean subprocess test and explicit import |
| MM-TEST-01 | Maintainability/Reliability | Medium / BAD-PRACTICE | Validator candidate winner changed under load because mutable `updated_at` controlled processing order | Phase 0/R4.4 | RESOLVED | Deterministic timestamp-inversion regression passes; targeted lifecycle suite 35 passed; full isolated non-ML gate 3,094 passed, 56 skipped, 95 deselected, 24 intentional xfails |

## Phase 1 budget reconciliation (2026-07-12)

This supplement is authoritative for the six rows in the V3 Phase 1 scope. It
does not change or close Phase 2-4 work.

| Finding | Phase 1 disposition | Final evidence |
|---|---|---|
| MM-SEC-01 | `BLOCKED-EXTERNAL` | R1.1-R1.2 repository work remains complete and fail-closed. Disposable two-role Postgres proof and brownfield inventory/repair approval are still external; the Team/Postgres profile stays disabled. |
| MM-SEC-02 | `IN-PROGRESS` overall; Phase 1 containment complete | R1.3 continues to deny every Qdrant payload-read adapter. R1.5 adds authenticated, TLS-verified, redirect-denying maintenance transport. Required ML gate: 38 passed. R2.1 governed rehydration remains backlog and semantic reads stay disabled. |
| MM-SEC-03 | `BLOCKED-EXTERNAL` | New writes and inventory code are covered; the live read-only inventory accounted for SQLite/artifact/spool surfaces but reported legacy unscannable/truncated material and no configured Qdrant. No cleanup or redaction was authorized. |
| MM-SEC-04 | `RESOLVED` | Shared persisted-envelope gateway covers auxiliary writers; focused final integrity gate passed 46 tests with 1 environment skip. |
| MM-OPS-01 | `BLOCKED-EXTERNAL` | Fixed credentials were removed and Compose is fail-closed/private. Historical rotation/recreation and an external port probe remain operator work. |
| MM-OPS-04 | `IN-PROGRESS` overall; Phase 1 defaults complete | Loopback bindings, digest-only images, Qdrant API-key/TLS propagation, redirect denial, clean-wheel/SBOM binding, and scanner policy are committed. Approved local product images and runtime targets were unavailable; R3.4 remains backlog. |

## Source-domain reconciliation

The canonical rows above deduplicate repeated findings from the 13 domain reports. Original domain IDs remain traceable as follows:

- Domain 01 F-1.1..F-1.5 -> MM-SEC-01..04, MM-OPS-04.
- Domain 02 F-2.1..F-2.6 -> MM-ARCH-01..02, MM-MAINT-01..02.
- Domain 03 F-3.1..F-3.6 -> MM-SEC-01..02, MM-ARCH-01, MM-LIFE-01, MM-PERF-04, MM-DB-01.
- Domain 04 F-4.1..F-4.7 -> MM-OPS-01..04, MM-UX-01, MM-OBS-01.
- Domain 05 F-5.1..F-5.6 -> MM-PERF-01..04, MM-REL-02, MM-COST-03.
- Domain 06 F-6.1..F-6.7 -> MM-UX-01..03.
- Domain 07 F-7.1..F-7.8 -> MM-SEC-02, MM-ARCH-01, MM-REL-02..03, MM-UX-01..02, MM-COST-03, MM-OPS-02.
- Domain 08 F-8.1..F-8.6 -> MM-PRIV-01..02, MM-SEC-03..04.
- Domain 09 F-9.1..F-9.5 -> MM-UX-01, MM-MAINT-01..02, MM-OPS-03.
- Domain 10 F-10.1..F-10.6 -> MM-COST-01..03, MM-PERF-01..03.
- Domain 11 F-11.1..F-11.6 -> MM-OPS-01..04, MM-DEMO-01, MM-ARCH-01, MM-UX-01.
- Domain 12 F-12.1..F-12.5 -> MM-OPS-05, MM-OBS-01, MM-COST-03, MM-PRIV-02.
- Domain 13 F-13.1..F-13.9 -> MM-ARCH-01..02, MM-PRIV-01, MM-DEMO-01, MM-SEC-01, MM-LIFE-01, MM-INTEGRITY-01, MM-MAINT-01..02.

## Rollback discipline

- Tests/docs: revert the atomic work-package commit.
- Policy changes: disable the affected blocked profile; never restore an unsafe broad default.
- Schema changes: restore a verified backup or apply the documented forward repair; immutable migrations are not edited in place.
- Qdrant: disable semantic mode and fall back to authoritative lexical retrieval.
- Capture: return to quiet/no-capture, not the legacy unlimited Stop-hook behavior.
