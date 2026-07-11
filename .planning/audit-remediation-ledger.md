# Audit Remediation Ledger

**Baseline commit:** `9c2e2bf4b10c9acbbb5ed5832af730b3b3ca851a`
**Roadmap:** `.planning/REMEDIATION-OPTIMIZATION-PLAN-2026-07-10.md`
**Baseline audit:** `.planning/audits/2026-07-10-baseline/audit-report.md`

Statuses: `OPEN`, `IN-PROGRESS`, `RESOLVED`, `BLOCKED-EXTERNAL`, `BLOCKED-POLICY`.

| ID | Primary domain | Severity / exploitability | Summary | Package | Status | Acceptance evidence |
|---|---|---|---|---|---|---|
| MM-SEC-01 | Security/Database | Critical / EXPLOITABLE-NOW / H1 | MCP roles, project scopes, and Postgres tenant isolation are not enforced centrally | R1.1-R1.2 | OPEN | Reader denial, cross-project/tenant matrix, Postgres RLS tests |
| MM-SEC-02 | Security/Database | Critical / EXPLOITABLE-NOW / H1 | Qdrant can return archived, sensitive, cross-scope/tenant, or orphan payloads | R1.3,R2.1 | OPEN | Fake and real Qdrant authoritative-filter tests |
| MM-OPS-01 | DevOps | Critical / EXPLOITABLE-LOW-EFFORT / H4 | Postgres Compose publishes a fixed credential | R1.5 | OPEN | Fail-closed Compose config, private port, external rotation evidence |
| MM-SEC-03 | Security | High / EXPLOITABLE-NOW | Persisted metadata/provenance fields bypass sensitivity scanning | R1.4 | OPEN | Complete field-matrix adversarial suite and legacy dry-run |
| MM-SEC-04 | Security | High / EXPLOITABLE-NOW | Steward, compact-summary, verbatim, and integration writes bypass one gateway | R1.4 | OPEN | Table-driven write-path test; no raw fixture in durable scan |
| MM-ARCH-01 | Architecture/Database | High / BAD-PRACTICE | Entity registry and graph own incompatible `entities` schemas | R2.3 | OPEN | Normal init-to-graph MCP integration and FK check |
| MM-ARCH-02 | Architecture/Integrity | High / BAD-PRACTICE | MCP/hooks/CLI/Qdrant use contradictory retrieval planners and trust defaults | R2.1 | OPEN | Cross-surface ID-set and conversational-query parity |
| MM-LIFE-01 | Database/Reliability | High / BAD-PRACTICE | Scheduled archival uses raw SQL and bypasses lifecycle/vector evidence | R2.2 | OPEN | Version/event/timestamp/outbox assertions through scheduled path |
| MM-REL-02 | Reliability/Performance | Medium / BAD-PRACTICE | MCP reads take write locks and context detail modes retrieve twice | R2.2 | OPEN | Zero query write lock; one aggregated telemetry envelope |
| MM-OPS-02 | DevOps/Demo | High / BAD-PRACTICE | Docker/Helm publish HTTP on a stdio MCP process and use an invalid healthcheck | R3.4 | OPEN | Built-container readiness and MCP handshake |
| MM-OPS-03 | DevOps/Maintainability | High / BAD-PRACTICE | A release tag can publish without a blocking verified test artifact | R4.4 | OPEN | Deliberately failing release candidate cannot publish |
| MM-OPS-04 | Security/DevOps | High / EXPLOITABLE-LOW-EFFORT | Qdrant/Ollama ports are broadly exposed; images are mutable | R1.5,R3.4 | OPEN | Private network defaults, auth/TLS, pinned digests |
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
