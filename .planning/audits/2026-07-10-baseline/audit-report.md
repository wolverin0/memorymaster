# MemoryMaster Technical Due-Diligence Baseline

**Audited:** MemoryMaster at `9c2e2bf4b10c9acbbb5ed5832af730b3b3ca851a`
**Stack:** Python 3.10+, FastMCP stdio, SQLite FTS5/WAL, optional Postgres/Qdrant/Kuzu, BaseHTTPRequestHandler dashboard
**Audit date:** 2026-07-10
**Materialized:** 2026-07-11 in isolated remediation worktree
**Production LoC scanned:** 50,099 nonblank lines across 272 files
**Tests:** 47,441 nonblank lines across 270 files; 3,245 collected
**Inventory:** `inventory.json`

This report materializes the full audit completed immediately before the remediation goal. The baseline commit is unchanged; Phase 0 rechecked inventory, isolated imports, GitNexus freshness, tests, and live read-only metrics.

Baseline verification in the isolated worktree:

- Ruff: clean.
- Collection: 3,245 tests.
- Non-ML run: 3,092 passed, 56 skipped, 95 deselected, 1 expected failure; one steward CLI subprocess exceeded its fixed 30-second test timeout during the full run.
- Isolated rerun of the timed-out steward test: passed in 25.77 seconds.
- The same non-ML suite had passed 3,093 tests immediately before isolation. The timeout is recorded as baseline flakiness, not silently treated as a product failure or a clean full-run pass.

## Hard stops

Three launch-blocking conditions are present:

1. **H1 / MM-SEC-01:** Standard MCP operations do not enforce agent/project/tenant boundaries; team Postgres has no RLS fallback.
2. **H1 / MM-SEC-02:** Qdrant bypasses authoritative lifecycle/scope/tenant/sensitivity filtering.
3. **H4 / MM-OPS-01:** The documented Postgres Compose profile publishes a known default credential.

## Audit verdict

**DO NOT LAUNCH UNTIL HARD STOPS RESOLVED**

Local trusted-agent SQLite use is materially safer than the blocked team, Qdrant, Docker/Helm, and full-stack profiles.

## Severity census

Canonical deduplicated findings are tracked in `.planning/audit-remediation-ledger.md`.

| Severity | Count |
|---|---:|
| Hard stops | 3 |
| Critical | 3 |
| High | 19 |
| Medium | 11 |
| Low | 0 |

| Exploitability | Count |
|---|---:|
| EXPLOITABLE-NOW | 8 |
| EXPLOITABLE-LOW-EFFORT | 2 |
| BAD-PRACTICE | 23 |
| UNKNOWN | 0 |

Tambon density: 0.021 findings per 1,000 reviewed production+test LoC. The defect pattern is integration-seam drift, not pervasive generated-code nonsense.

## Strengths to preserve

- WAL, foreign keys, busy timeouts, and connection retry are centralized for core stores (`memorymaster/stores/_storage_shared.py:76-136`).
- Lifecycle transitions couple optimistic version checks with append-only events (`memorymaster/stores/_storage_lifecycle.py:59-106`).
- Ingest has bitemporal validation, deduplication, citations, and a central sanitizer (`memorymaster/core/service.py:472-694`).
- Steward phases are budgeted and failure-isolated (`memorymaster/core/service.py:731-883`).
- Snapshot/restore, integrity jobs, query introspection, and extensive temp-SQLite tests provide a strong safety foundation.

## Remediation source of truth

- Roadmap: `.planning/REMEDIATION-OPTIMIZATION-PLAN-2026-07-10.md`
- Finding ledger: `.planning/audit-remediation-ledger.md`
- Red-test matrix: `.planning/PHASE0-RED-TEST-MATRIX.md`
- Operating envelope: `.planning/OPERATING-ENVELOPE-2026-07-11.md`
- External actions: `external-actions-required.md`

---

## Domain 01 — Security

### Founder view

The local loopback product has good security primitives, but team boundaries are promises rather than enforced controls. Several write paths also persist secrets outside the main filter.

### Technical evidence

- **MM-SEC-01 — Critical / EXPLOITABLE-NOW / H1.** Roles exist (`memorymaster/core/access_control.py:35-46`), but MCP ingest/list/mutations do not centrally require identity or permission (`memorymaster/surfaces/mcp_server.py:603-695`, `1510-1533`, `1643-1691`).
- **MM-SEC-02 — Critical / EXPLOITABLE-NOW / H1.** `_qdrant_query` accepts arbitrary hits and raw orphan payloads (`memorymaster/surfaces/mcp_server.py:455-513`); the early return skips normal policy flags (`1030-1037`).
- **MM-SEC-03 — High / EXPLOITABLE-NOW.** The sanitizer omits persisted holder/source-agent/key and citation-source/locator fields (`memorymaster/core/security.py:475-524`, `550-558`).
- **MM-SEC-04 — High / EXPLOITABLE-NOW.** Verbatim, steward update, and compact-summary paths do not all pass one complete write gateway (`memorymaster/recall/verbatim_store.py:136-176`, `memorymaster/govern/llm_steward.py:730-812`, `memorymaster/govern/jobs/compact_summaries.py:347-380`).

[SECTION COMPLETE: Domain 01]

## Domain 02 — Architecture and code quality

### Founder view

The package split is directionally sound, but retrieval and entity identity have competing authorities. Large facades and hardwired optional features magnify that drift.

### Technical evidence

- **MM-ARCH-01 — High / BAD-PRACTICE.** The registry defines integer/canonical entities (`memorymaster/knowledge/entity_registry.py:150-190`) while the graph expects text/name/type entities in the same table (`memorymaster/knowledge/entity_graph.py:109-154`).
- **MM-ARCH-02 — High / BAD-PRACTICE.** Prompt recall tokenizes/fans out (`memorymaster/recall/context_hook.py:1364-1430`), MCP sends raw queries and has a separate Qdrant path (`memorymaster/surfaces/mcp_server.py:455-513`, `1039-1048`), and context defaults disagree (`1195-1208`, `memorymaster/core/service.py:1431-1485`).
- **MM-MAINT-02 — Medium / BAD-PRACTICE.** `MemoryService` spans `memorymaster/core/service.py:352-2200`; the MCP and CLI surfaces register 36 tools and 106 parser entries, while `memorymaster/core/plugins.py:1-5` says its plugin seam has no live consumers.

[SECTION COMPLETE: Domain 02]

## Domain 03 — Database and data layer

### Founder view

SQLite fundamentals are strong. Shared Postgres, Qdrant, entity schema, and scheduled lifecycle paths do not preserve the same truth boundary.

### Technical evidence

- **MM-SEC-01 — Critical / EXPLOITABLE-NOW / H1.** MCP constructs a service without tenant identity (`memorymaster/surfaces/mcp_server.py:322-325`); Postgres adds its tenant predicate only when non-null (`memorymaster/stores/postgres_store.py:555-570`), and the schema has no RLS policies.
- **MM-LIFE-01 — High / BAD-PRACTICE.** The scheduled hook archives with raw SQL (`memorymaster/config_templates/hooks/memorymaster-steward-cycle.py:46-61`) instead of the versioned/evented transition (`memorymaster/stores/_storage_lifecycle.py:59-106`).
- **MM-PERF-04 — Medium / BAD-PRACTICE.** Event reads filter by type/details (`memorymaster/stores/_storage_read.py:348-369`) but schemas index claim/time rather than those predicates (`memorymaster/schema.sql:196-199`).
- **MM-DB-01 — Medium / BAD-PRACTICE.** The optional schema fast path fingerprints migrations/schema files, while many legacy ensure mutations remain outside them (`memorymaster/stores/storage.py:55-71`, `105-165`).

[SECTION COMPLETE: Domain 03]

## Domain 04 — Infrastructure and DevOps

### Founder view

CI coverage is substantial, but the advertised container and team deployment contracts are not functional or safe enough to ship.

### Technical evidence

- **MM-OPS-01 — Critical / EXPLOITABLE-LOW-EFFORT / H4.** `docker-compose.postgres.yml:6-11` publishes Postgres with a fixed credential; `INSTALLATION.md:140-146` presents it as a normal profile.
- **MM-OPS-02 — High / BAD-PRACTICE.** The image exposes 8765 but starts stdio MCP (`Dockerfile:34-38`, `memorymaster/surfaces/mcp_server.py:1979-1983`); Compose health-checks an unsupported version option (`docker-compose.yml:25-26`).
- **MM-OPS-03 — High / BAD-PRACTICE.** Tag publication runs build/metadata checks without a blocking test dependency (`.github/workflows/publish.yml:25-84`).
- **MM-OPS-04 — High / EXPLOITABLE-LOW-EFFORT.** Qdrant/Ollama ports and mutable images are broadly exposed in `docker-compose.yml:32-51`; Helm also defaults to `latest` (`helm/memorymaster/values.yaml:3-6`).

[SECTION COMPLETE: Domain 04]

## Domain 05 — Performance

### Founder view

Current scale is workable, but reads secretly perform repeated embeddings and writes. Multi-pane load therefore amplifies contention and cost.

### Technical evidence

- **MM-PERF-01 — High / BAD-PRACTICE.** Hybrid retrieval overfetches candidates (`memorymaster/core/service.py:1139-1167`) and unconditionally embeds/upserts them during vector scoring (`memorymaster/stores/_storage_lifecycle.py:488-560`).
- **MM-REL-02 — Medium / BAD-PRACTICE.** MCP creates read-write services (`memorymaster/surfaces/mcp_server.py:322-325`), reads record access/feedback (`memorymaster/core/service.py:1323-1369`), and non-standard context detail levels retrieve twice (`memorymaster/surfaces/mcp_server.py:1221-1255`).
- **MM-PERF-03 — High / EXPLOITABLE-NOW when enabled.** Reconciliation caps each status at 10,000 and embeds sequentially (`memorymaster/recall/qdrant_backend.py:341-370`).
- **MM-PERF-02 — Medium / BAD-PRACTICE.** Tokenizer initialization scans and process-caches the active corpus without a generation key (`memorymaster/recall/recall_tokenizer.py:182-234`).

[SECTION COMPLETE: Domain 05]

## Domain 06 — UX and accessibility

### Founder view

The dashboard exposes valuable governance evidence, but setup and review actions can falsely imply success. Error, accessibility, and narrow-screen states need a dedicated pass.

### Technical evidence

- **MM-UX-01 — High / BAD-PRACTICE.** Setup verification checks only a local ingest/query sentinel (`memorymaster/surfaces/setup_hooks.py:623-677`) yet can print completion and return success (`900-936`).
- **MM-UX-02 — High / BAD-PRACTICE.** A review row is removed before the POST resolves (`memorymaster/surfaces/dashboard.py:1067`).
- **MM-UX-03 — Medium / BAD-PRACTICE.** Initial panel failures are swallowed (`memorymaster/surfaces/dashboard.py:1078`); multiple inputs lack labels and fixed grids lack responsive stacking (`976-1013`, `912`, `1051`).

[SECTION COMPLETE: Domain 06]

## Domain 07 — Reliability

### Founder view

Core SQLite recovery is strong. Subsystem seams fail open or report healthy states when graph, vector, setup, or worker operations are incomplete.

### Technical evidence

- **MM-SEC-02/MM-LIFE-01 — High.** Count-only vector reconciliation and raw lifecycle changes allow stale truth to survive (`memorymaster/govern/jobs/qdrant_reconcile.py:142-155`, scheduled hook `46-61`).
- **MM-REL-03 — Medium / BAD-PRACTICE.** Media jobs transition pending to retrying but have no stale-lease reclamation (`memorymaster/stores/_storage_sources.py:626-764`).
- Process-local quotas reset across stdio processes (`memorymaster/core/intake_policy.py:151-180`, `memorymaster/surfaces/mcp_server.py:45-62`).

[SECTION COMPLETE: Domain 07]

## Domain 08 — Privacy and compliance signals

### Founder view

Provenance and redaction primitives are good, but there is no complete consent, retention, export, or erasure lifecycle across the many secondary copies.

### Technical evidence

- **MM-PRIV-01 — High / BAD-PRACTICE.** Integration docs say distilled session-end ingest (`docs/INTEGRATING.md:21-25`, `63-66`), while the default Stop hook stores transcript data and invokes extraction every stop (`memorymaster/config_templates/hooks/memorymaster-auto-ingest.py:304-321`).
- **MM-PRIV-02 — High / BAD-PRACTICE.** Claims archive rather than delete and event retention is a no-op (`memorymaster/stores/_storage_lifecycle.py:109-128`, `249-251`); verbatim/Atlas/Qdrant/artifacts are outside a subject-wide workflow.

[SECTION COMPLETE: Domain 08]

## Domain 09 — Maintainability and developer experience

### Founder view

Testing and documentation effort are unusually strong, but contributors cannot trust setup PASS, version output, release counts, or stated complexity limits.

### Technical evidence

- **MM-MAINT-01 — Medium / BAD-PRACTICE.** Package version is 4.4.1 (`pyproject.toml:7`), module version is 4.0.0 (`memorymaster/__init__.py:5`), the dashboard renders v1.0.0 (`memorymaster/surfaces/dashboard.py:971`), and docs disagree on tool counts.
- **MM-MAINT-02 — Medium / BAD-PRACTICE.** Written sub-800/sub-50 limits are not enforced while major facades exceed them; compatibility aliases remain after their stated window.
- **MM-INTEGRITY-01 — Medium / BAD-PRACTICE.** Setup detection imports `importlib` but accesses `importlib.util` (`memorymaster/surfaces/setup_detect.py:154-161`); its test preloads/masks the attribute (`tests/test_setup_detect.py:236-249`).
- **MM-TEST-01 — Medium / BAD-PRACTICE.** Two isolated full-suite runs failed at different tests (`tests/test_steward.py:215` timeout; `tests/test_sqlite_core.py:245` winner assertion), while each passed immediately alone. The baseline gate is order/load-sensitive and remains under diagnosis.

[SECTION COMPLETE: Domain 09]

## Domain 10 — Cost

### Founder view

Budget controls exist, but the busiest default capture and embedding paths bypass finite persisted limits. Live storage demonstrates the consequence.

### Technical evidence

- **MM-COST-01 — High / EXPLOITABLE-NOW.** Stop hooks can call paid providers every stop (`memorymaster/config_templates/hooks/memorymaster-auto-ingest.py:304-321`); caps default unlimited and only apply inside explicit scopes (`memorymaster/core/llm_budget.py:11-17`, `memorymaster/core/llm_provider.py:694-705`).
- **MM-COST-02 — High / EXPLOITABLE-NOW.** Each stop reparses/replays the transcript (`memorymaster/recall/verbatim_store.py:227-322`) and cleanup has no age/byte/session policy (`memorymaster/govern/verbatim_cleanup.py:127-204`).
- **MM-COST-03 — Medium / BAD-PRACTICE.** External model calls and quotas do not share one persisted account-wide ledger.

[SECTION COMPLETE: Domain 10]

## Domain 11 — Demo versus production

### Founder view

The governed local core is more mature than several advertised integrations. The most dangerous demo behavior is mock media output becoming high-confidence evidence.

### Technical evidence

- **MM-DEMO-01 — High / EXPLOITABLE-NOW.** CLI transcription/OCR defaults to mock (`memorymaster/surfaces/cli.py:175-183`); fabricated output is assigned 0.99 and persisted (`memorymaster/bridges/media_processing.py:44-75`, `145-153`).
- Entity graph, full-stack setup, and container profiles are exposed despite the runtime failures documented above.

[SECTION COMPLETE: Domain 11]

## Domain 12 — Missing production capabilities

### Founder view

Local health, integrity, and snapshot primitives exist. Team operation lacks off-device recovery, central error reporting, attributable administration, and governed configuration.

### Technical evidence

- **MM-OPS-05 — High / BAD-PRACTICE.** Snapshots default to the same machine and retain three copies (`memorymaster/stores/snapshot.py:151-175`); non-SQLite backup is skipped (`memorymaster/govern/jobs/integrity.py:270`).
- **MM-OBS-01 — High / BAD-PRACTICE.** Metrics live in process memory or local files (`memorymaster/core/observability.py:12`, `memorymaster/surfaces/metrics_exporter.py:301`), without central tracing/error ownership.
- Audit events do not consistently carry principal/tenant/request identity (`memorymaster/schema.sql:83`, `memorymaster/surfaces/dashboard.py:1542-1546`).

[SECTION COMPLETE: Domain 12]

## Domain 13 — Code integrity and coherence

### Founder view

Low Tambon density shows the code is reviewed. Failures cluster where independently tested planes disagree: entities, retrieval, RBAC, capture, lifecycle, and release identity.

### Technical evidence

- Registry and graph tests use isolated schemas, masking composition failure (`tests/test_entity_graph.py:62-84`).
- RBAC workflow tests call the helper rather than exercising MCP denial, masking MM-SEC-01.
- Stop-hook behavior contradicts the integration contract and creates overlapping capture authorities.
- The plugin registry is documented complete despite zero live consumers (`memorymaster/core/plugins.py:1-5`).

[SECTION COMPLETE: Domain 13]

---

## Audit method and attestation

The audit used method/inventory, hard-stop, Tambon, blind-spot, and 13 isolated domain reviews. Runtime probes used temporary databases/fakes; live metrics were read-only. Inventory completeness: 28/28 HTTP routes, 36/36 MCP tools, 6/6 migrations, 0/0 Supabase tables, 151/151 environment controls, and 36/36 binary flags.

- R1 Evidence or silence: all canonical findings cite current source.
- R2 Quote before cite: cited ranges were directly inspected.
- R3 Severity honesty: H1/H4 lock the verdict.
- R4 Exploitability clarity: every security/data finding is tagged.
- R5 Prompt-injection immunity: repository instructions were treated as project governance, not audit-result overrides; no malicious injection found.
- R6 Completion discipline: all 13 domains have completion markers.
- R7 Stack honesty: Python/FastMCP/SQLite/Postgres/Qdrant conventions only.

[AUDIT COMPLETE: all 7 rules attested, all 13 domains covered]
