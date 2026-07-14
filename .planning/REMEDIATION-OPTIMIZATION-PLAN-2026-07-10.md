# MemoryMaster Remediation & Optimization Plan

**Status:** PROPOSED — ready for execution
**Created:** 2026-07-10
**Scope:** `memorymaster/`, tests, packaging, setup, Docker/Helm, operational docs
**Source:** 2026-07-10 full audit (hard stops, blind spots, Tambon, 13 domains, runtime probes)
**Execution model:** test-first, atomic work packages, audit-loop convergence

## 1. Objective

Make MemoryMaster's public surfaces preserve the guarantees already present in its governed core:

1. A caller cannot cross agent, project, scope, visibility, or tenant boundaries.
2. Every persisted field passes one sensitivity and provenance policy.
3. SQLite/Postgres is authoritative; Qdrant is a derived candidate index only.
4. Trusted recall returns active confirmed truth by default.
5. Capture, LLM calls, storage growth, and reconciliation are finite and observable.
6. Every advertised setup/deployment profile works end to end and fails closed.
7. Optional integrations cannot silently create fake or ungoverned evidence.

This is a hardening and convergence program, not a rewrite. Preserve the working lifecycle, citation, WAL, event-ledger, snapshot, steward, and retrieval-explanation foundations.

## 2. Release posture during remediation

| Profile | Current posture | Promotion gate |
|---|---|---|
| Local SQLite, trusted agents | Usable with caution | Trusted recall, complete sensitivity gateway, quiet capture |
| Semantic/Qdrant | BLOCKED | Authoritative rehydration/filtering and exact reconciliation |
| Team/Postgres | BLOCKED | Authenticated tenant context, MCP authorization, RLS, adversarial isolation tests |
| Docker/Helm/full stack | BLOCKED | Correct entrypoints, secrets, network defaults, readiness, container smoke |
| Atlas media/entity graph | EXPERIMENTAL | No default mocks; unified entity schema; composed integration tests |

Do not describe a blocked profile as production-ready until its gate is demonstrated with runtime evidence.

## 3. Program invariants

- Query MemoryMaster before each architectural decision and ingest verified non-obvious conclusions.
- MemoryMaster `query_memory` access telemetry and narrowly scoped `ingest_claim`/`checkpoint` writes required by project governance are authorized during execution. This exception does not authorize cycles, compaction, cleanup, redaction, backlog mutation, migrations, or other live-data operations.
- Run GitNexus impact analysis before editing every function, class, or method. Warn before HIGH/CRITICAL blast-radius changes.
- Run `gitnexus_detect_changes()` before each commit.
- Check GitNexus index freshness in Phase 0. After every commit, rerun `npx gitnexus analyze --embeddings` when the existing index contains embeddings before doing further impact analysis.
- Preserve all pre-existing dirty-worktree changes; stage and commit only explicit remediation paths.
- Prefer an isolated worktree with a dedicated virtual environment. Before testing, prove `memorymaster.__file__` resolves into that worktree; otherwise stop using it and avoid the editable-install import-pin trap.
- Use temporary databases and fake/local services until a phase's live-migration gate is approved.
- No production credential rotation, push, publish, deployment, or live database mutation without explicit authority.
- **WARNING LEGAL REVIEW REQUIRED:** a `BLOCKED-POLICY` or `ACCEPT` disposition requires the `audit-decisions` workflow, legal sign-off, and a complete, approved, unexpired `baseline-policy.md` entry. This does not pause remediation.
- Schema changes update SQLite, Postgres, versioned migrations, parity tests, and documentation together.
- Every security defect gets a failing adversarial test before its implementation fix.
- One logical work package per conventional commit. Keep rollback possible after every package.
- Do not weaken the sensitivity filter, lifecycle, event ledger, WAL discipline, or citation requirements to make tests pass.

## 4. Baseline and success metrics

Record these again at execution start because the live database changes continuously.

| Metric | Audit baseline | Completion target |
|---|---:|---:|
| Non-ML tests | 3,093 passed | All pass after every phase |
| Ruff | Clean | Clean after every commit |
| Claims | ~108,000 | Informational |
| Candidate backlog | ~21,700 | Intake <= stewardship capacity; no silent auto-confirmation |
| Candidates older than 30 days | ~18,500 | Explicitly classified by a reviewed burn-down policy |
| Verbatim rows | ~1.07 million | Retention policy active; incremental capture only |
| SQLite size | ~5.5 GB | Storage budget and alerting defined; no unbounded growth |
| Cross-tenant/project adversarial reads | Confirmed leak | Zero |
| Sensitive metadata persistence | Confirmed leak | Zero for all persisted string fields |
| Qdrant archived/orphan returns | Confirmed | Zero |
| Warm repeated hybrid query | Re-embeds/rewrites candidates | Zero candidate embedding calls and zero DB writes |
| Natural-language MCP recall | Can return zero despite keyword hit | Same planner/normalization as prompt recall |

Phase 0 must replace qualitative capacity goals with recorded numeric gates in the remediation ledger:

- trailing-14-day candidate inflow/day and steward dispositions/day;
- candidate inflow <= 80% of measured steward disposition capacity for seven consecutive days;
- candidate-age P95 <= 7 days after the approved backlog campaign;
- explicit retention limits by age, bytes, and sessions;
- maximum daily DB growth in MiB and warning/critical disk watermarks;
- a dated backlog completion criterion with counts for confirmed, archived, rejected, and still-reviewable claims.

## 5. Work packages

### Program tracker

- [x] Phase 0 — evidence, numerical gates, and red tests
- [x] R1.1 — authenticated request context and MCP authorization
- [x] R1.2 — Postgres tenant enforcement and RLS (repository complete; external proof blocked)
- [x] R1.3 — immediate Qdrant containment
- [x] R1.4 — complete sensitivity/write gateway and legacy inventory (repository complete; live cleanup/Qdrant inventory blocked externally)
- [x] R1.5 — secure deployment and supply-chain defaults (repository complete; rotation, advisory/image/runtime evidence blocked externally)
- [x] Phase 1 targeted budget audit delta (V3 scope; Phases 2-4 remain open)
- [x] R2.1 — unified RetrievalPlanner and governed Qdrant reintegration
- [x] R2.2 — lifecycle authority and read-only recall
- [x] R2.3 — unified entity model
- [x] R2.4 — quiet capture, budgets, retention, and backlog control (repository scope; legal review and account-wide cost convergence continue in R3)
- [x] R2.5 — remove fake evidence defaults
- [x] Phase 2 targeted convergence and audit delta
- [x] R3.1 — embedding and reconciliation efficiency
- [x] R3.2 — query and storage efficiency
- [x] R3.3 — truthful setup profiles
- [x] R3.4 — service entrypoints and deployment health
- [x] R3.5 — recovery, observability, audit, and privacy operations
- [x] R4.1 — core/extension boundaries
- [x] R4.2 — decompose oversized orchestration points
- [x] R4.3 — governance UX and accessibility
- [x] R4.4 — generated release/documentation truth
- [ ] Final audit delta and convergence evidence

### Phase 0 — Freeze evidence and build the red test matrix

**Goal:** Preserve the audit evidence and prevent false fixes.

- [x] **R0.1 — Execution isolation**
  - Capture `git status`, branch, Python path, installed package version, DB size/counts, and relevant service versions.
  - Establish an isolated branch/worktree and dedicated virtual environment, or document why the main checkout is required.
  - Verify imports point at the intended checkout.
- [x] **R0.2 — Finding ledger**
  - Create `audit-remediation-ledger.md` mapping every audit finding to owner, package, status, commit, verification evidence, and rollback.
  - Deduplicate cross-domain findings without losing source-domain traceability.
- [x] **R0.3 — Adversarial regression fixtures**
  - Reader-agent mutation denial.
  - Cross-project and cross-tenant list/query/mutation denial.
  - Archived, sensitive, wrong-scope, wrong-tenant, and orphan Qdrant hits.
  - Secrets in every persisted string field and automated write path.
  - Registry-initialized entity graph.
  - Conversational query parity across MCP, hooks, CLI, and context packing.
  - Container entrypoint/readiness contract.
- [x] **R0.4 — Numerical operating envelope**
  - Measure and write the required capacity, retention, DB-growth, disk-watermark, and backlog-completion numbers into the ledger.
  - Record the measurement query/window and make every later performance/cost gate consume those frozen values.

**Exit gate:** Every Critical/High finding has a reproducible failing test or an evidence note explaining why a hermetic test is impossible, and every operating-envelope placeholder has a numeric value and measurement source.

---

### Phase 1 — P0 trust boundary and deployment hard stops

#### R1.1 — Central authenticated request context

Introduce one immutable request context containing principal, role, tenant, workspace, allowed scopes, and sensitive-data capability.

- Derive it at the MCP transport/process boundary; do not trust caller-supplied identity fields.
- Map every MCP tool to a named authorization action through one decorator/helper.
- Require context for all read and mutation tools in shared/team mode.
- Apply scope/tenant policy to list, query, graph, lineage, export, pin, redact, compact, steward, and configuration operations.
- Keep local trusted-agent mode explicit rather than accidentally unauthenticated.

**Acceptance:** reader writes fail; unauthorized operations cause no domain-state mutation (an append-only denial audit event is the only permitted write); project/tenant A cannot enumerate or retrieve B.

#### R1.2 — Postgres tenant enforcement and RLS

- Make tenant context mandatory when the team/Postgres profile is enabled.
- Add restrictive Postgres RLS/policies as defense in depth.
- Ensure direct ID lookups, joins, graph paths, events, citations, exports, and maintenance jobs carry tenant predicates.
- Add real Postgres adversarial tests gated by `MEMORYMASTER_TEST_POSTGRES_DSN`.

**Acceptance:** tenant-isolation matrix passes through service, MCP, Qdrant, and direct store calls; missing tenant fails closed.

#### R1.3 — Immediate Qdrant containment

- Remove raw-payload fallback when the authoritative row is missing.
- Disable/fail closed on Qdrant retrieval whenever authenticated policy context or authoritative rehydration is unavailable.
- Keep the semantic profile blocked until R1.1 and R2.1 reintegrate it through the shared planner.

**Acceptance:** the vulnerable Qdrant fast path is unreachable; no raw/orphan payload can be returned; authoritative lexical retrieval remains available as a safe fallback.

#### R1.4 — Complete persisted-envelope sensitivity gateway

- Inventory every persisted string/JSON field across claims, citations, events, verbatim, feedback, Atlas source/evidence, artifacts, and Qdrant payloads.
- Scan/reject/redact `holder`, `source_agent`, idempotency key, scope/type identifiers where appropriate, citation source/locator, payload JSON, and provenance metadata.
- Route all claim creation and updates—including steward, compact summaries, miners, imports, and bridges—through one write gateway.
- Apply encoded/decoded secret detection consistently to verbatim and metadata.
- Preserve a safe read-time legacy detector for pre-existing rows.
- Produce a dry-run inventory of potentially sensitive legacy metadata and Qdrant payloads, with quarantine/redaction/rebuild steps. Live cleanup requires an approved backup and explicit authority.

**Acceptance:** a table-driven adversarial suite covers every persisted field and write path; no secret-shaped fixture reaches durable storage or derived indexes; the legacy dry-run accounts for primary and derived copies.

#### R1.5 — Secure deployment defaults

- Replace fixed Postgres credentials with fail-closed secret interpolation.
- Do not publish Postgres, Qdrant, or Ollama on non-loopback interfaces by default.
- Support Qdrant API key/TLS throughout the backend.
- Pin tested image versions/digests.
- Rotate/recreate any deployment known to have used the old database credential as an external action.
- Add repository/history secret scanning, container-image vulnerability scanning, and an SBOM for release artifacts.

**Acceptance:** Compose config fails without required secrets; external port probes fail by default; authenticated internal health checks pass.

**Phase 1 exit gate:** Audit hard stops H1/H4 are absent or the affected profile is disabled fail-closed; targeted security tests, Postgres isolation tests, Qdrant-containment tests, secret/history scan, image scan/SBOM, non-ML suite, Ruff, and dependency audit pass. The semantic profile remains blocked until R2.1.

---

### Phase 2 — P1 governed core convergence

#### R2.1 — One RetrievalPlanner and explicit trust modes

- Route MCP, context hooks, CLI, dashboard, task briefing, volunteer context, and Qdrant through one planner.
- Planner owns query normalization, candidate generation, policy filtering, fusion, ranking, limits, and telemetry.
- Default `trusted` mode: active confirmed/pinned claims only.
- Explicit `exploratory` mode: candidates/stale/conflicted with conspicuous annotations.
- Ensure documented hybrid/legacy defaults match runtime behavior.
- Reintegrate Qdrant only after R1.1 request context is available: overfetch IDs, rehydrate from SQLite/Postgres, and apply the same planner policy.
- Add server-side policy metadata plus primary-store post-filtering, exact ID/content-hash reconciliation, and a durable replayable outbox for upserts/deletes.

**Acceptance:** conversational and keyword forms retrieve equivalent relevant results; all surfaces return the same policy-filtered candidate set for the same request; no archived, sensitive, wrong-scope, wrong-tenant, or orphan Qdrant point is returned; equal-count/different-ID sets converge.

#### R2.2 — Lifecycle authority and read-only recall

- Replace scheduled raw-SQL archival with canonical lifecycle transitions.
- Ensure version increments, events, timestamps, optimistic locking, cache invalidation, and Qdrant deletion remain atomic/replayable.
- Make MCP/query surfaces read-only by default and spool one aggregated access/feedback envelope per top-level request.
- Remove the second retrieval executed by non-standard `query_for_context` detail levels.

**Acceptance:** one query records access at most once and takes no SQLite write lock; scheduled archive produces complete lifecycle evidence and vector deletion.

#### R2.3 — Unify the entity model

- Choose the canonical registry schema and write an immutable migration for graph edges/claim links.
- Remove lazy DDL from read-style MCP tools.
- Make graph readiness explicit in health output; do not return successful empty data on schema failure.
- Add registry-first, graph-first migration, backend parity, and full MCP integration tests.

**Acceptance:** normal `init_db` → extract entities → stats → related claims → enriched recall succeeds on one database with zero FK violations.

#### R2.4 — Quiet, finite capture and backlog control

- Make the documented three-beat loop the default: session-start fetch, on-demand recall, session-end/PreCompact distilled ingest.
- Put verbatim capture, per-stop extraction, correction mining, and stop-blocking behind explicit maximum-capture flags.
- Track a per-session transcript cursor; process only new turns.
- Add finite provider/global budgets and persisted usage accounting for every external model/embedding path.
- Add age/bytes/session retention and operator-visible storage watermarks.
- Define a dry-run, reviewable candidate-backlog burn-down policy; never bulk-confirm automatically.

**Acceptance:** repeated Stop events do not reprocess prior turns; caps survive process restarts; candidate inflow does not exceed measured steward capacity.

#### R2.5 — Remove fake evidence defaults

- Require an explicit real media provider for normal Atlas commands.
- Permit mocks only with a conspicuous test/dev flag and prevent mock evidence from producing governed claims/actions.

**Acceptance:** default production commands cannot persist fabricated transcript/OCR content.

**Phase 2 exit gate:** trusted recall and lifecycle invariants pass across every surface; entity features work on the real schema; capture/cost/retention benchmarks meet targets.

---

### Phase 3 — P2 performance, setup, and operational readiness

#### R3.1 — Embedding and reconciliation efficiency

- Persist model/content hashes and embed only missing or stale claims.
- Batch candidate embeddings and compute the query embedding once.
- Make warm repeated retrieval perform zero candidate embeddings and zero writes.
- Paginate Qdrant synchronization through all claims and persist a durable cursor.

#### R3.2 — Query and storage efficiency

- Add versioned SQLite/Postgres event indexes for actual `(event_type, details, created_at)` query shapes.
- Replace process-wide corpus scans with persisted/generation-keyed token statistics or token-specific FTS vocabulary queries.
- Clamp MCP limits, add cursors, remove duplicate serialization, and enforce durable quotas.
- Size Helm storage from retention metrics rather than the current fixed 1 GiB assumption.

#### R3.3 — Truthful setup profiles

- Provide `minimal`, `semantic`, `team`, and `full-lab` profiles.
- Emit component-level results: DB, MCP, recall hook, capture hook, provider, steward, vector backend, dashboard.
- Return nonzero when a requested component fails; distinguish `PASS`, `PARTIAL`, and `BLOCKED`.
- Ship required assets as package resources or remove unsupported wheel workflows.

#### R3.4 — Correct service entrypoints and health contracts

- Separate stdio MCP, streamable-HTTP MCP if supported, and dashboard entrypoints.
- Give each deployment profile real `/healthz` and `/readyz` behavior plus an MCP handshake smoke test.
- Add Docker/Helm end-to-end CI smoke and resource limits.

#### R3.5 — Recovery, observability, and privacy operations

- Add off-device encrypted backups, backend-aware Postgres recovery, backup-age alerts, and restore drills with documented RPO/RTO.
- SQLite backups must use the online-backup API or a fully quiesced DB+WAL snapshot, followed by restore and `PRAGMA integrity_check`; Postgres requires a consistent dump plus restore test before migration approval.
- Add optional OpenTelemetry/error tracking and persistent metrics/alerts for backlog, provider failures, DB integrity, WAL/disk, and stale backups.
- Add attributable audit envelopes with principal, tenant, role, request/session, action, target, and result.
- Implement inventory-driven privacy export/erase and retention propagation across primary DB, verbatim, Qdrant, artifacts, and documented backup expiry.

**Phase 3 exit gate:** clean-wheel setup passes each selected profile; Docker/Helm smoke is green; warm-query and sync benchmarks pass; backup restore and privacy dry-runs produce complete manifests.

---

### Phase 4 — P3 product focus and maintainability

#### R4.1 — Core/extension boundaries

- Keep claims, lifecycle, citations, policy, recall, conflict/stewardship, and telemetry in core.
- Move Wiki/Obsidian, Dream/OpenClaw, Atlas/media/actions, local search, and specialized bridges behind real entry-point extensions or companion packages.
- Wire the existing plugin API at explicit seams or remove it from the supported surface.

#### R4.2 — Decompose oversized orchestration points

- Retain `MemoryService` as a compatibility facade while extracting ingestion, retrieval, lifecycle, stewardship, telemetry, and integration services.
- Move dashboard read models/mutations out of HTTP handlers.
- Enforce a gradual size/complexity budget rather than a flag-day rewrite.
- Publish a dated removal plan for compatibility shims.

#### R4.3 — Human governance UX and accessibility

- Remove optimistic rows only after server success; add pending/error/retry states.
- Make panel failures distinct from empty states.
- Add labels/live regions, fix contrast, and stack layouts on narrow screens.
- Make conflict evidence, citations, lineage, rationale, and action consequences directly inspectable.

#### R4.4 — Generated truth for release/docs

- Derive runtime/package/dashboard version from one source.
- Generate MCP/CLI/test counts and feature/profile matrices in CI.
- Maintain one `Now / Next / Later / Not planned` roadmap.
- Make stable retrieval evaluation and release-critical tests blocking for publication.
- Publish only the artifact produced by a verified release workflow.

**Phase 4 exit gate:** extensions are explicit, core surfaces are smaller and policy-consistent, governance UI passes browser/a11y validation, and release/docs cannot drift silently.

## 6. Dependency order and safe parallelism

```text
R0 red tests
   ├── R1.1 identity/RBAC ──► R1.2 Postgres isolation
   ├── R1.3 Qdrant containment
   ├── R1.1 identity/RBAC ──► R2.1 unified retrieval + Qdrant reintegration ──► R3.1 performance
   ├── R1.4 write gateway ──► R2.4 capture/backlog
   └── R1.5 deployment ─────► R3.3/R3.4 setup and smoke

R2.2 lifecycle/read-only ───► R3.2 storage efficiency
R2.3 entity migration ──────► extension/product work
All P0/P1 gates ────────────► P3 decomposition and UI work
```

Safe parallel work is limited to packages with non-overlapping files and migrations. One agent owns each touched file. Security policy, `MemoryService`, MCP registration, store schemas, and migrations require serialized integration.

## 7. Verification ladder

Run the narrowest relevant tests after each edit, then these gates:

1. `ruff check memorymaster/`
2. Targeted adversarial and integration tests for the work package.
3. `python -m pytest tests/ -q --tb=short -m "not ml"`
4. `python -m pytest tests/ --co -q` and compare expected collection.
5. `pip-audit .` plus optional-extra audit where lockable.
6. Repository/history secret scan, container-image scan, and SBOM validation when deployment/release files change.
7. SQLite/Postgres backend parity and migration drift checks.
8. Qdrant policy/reconciliation integration tests.
9. ML-marked retrieval/embedding tests for R2.1/R3.1, or a documented `BLOCKED-EXTERNAL` item with owner/evidence requirement.
10. Clean-wheel install and selected-profile setup verification.
11. Docker/Compose/Helm config and runtime smoke.
12. Browser validation for dashboard mutations, errors, mobile layout, keyboard flow, and accessibility.
13. `gitnexus_detect_changes(scope="compare", base_ref=<phase-base>)` before commit/merge.
14. After commits, refresh the GitNexus index with `npx gitnexus analyze --embeddings` when embeddings exist.
15. Re-run the full audit against the same scope and produce a delta report.

Do not mark a checkbox complete from code inspection alone when the claim is runtime/deployment behavior.

## 8. Rollout and rollback

- Ship changes by profile and work package, not as one release.
- Team, Qdrant, and maximum-capture profiles remain disabled until their phase gates pass.
- Prefer fail-closed behavior for missing identity, tenant, secrets, or policy metadata.
- Qdrant may be disabled/fall back to authoritative lexical retrieval during rollout.
- Run schema migrations on backups/temp clones first; produce forward and rollback/recovery notes.
- Before touching the live DB: create a consistent off-device backup, restore it, run integrity checks, record counts/checksums, stop background writers if required, and obtain explicit approval.
- Any external credential rotation, firewall change, deployment, or provider-account budget is recorded in `external-actions-required.md` with owner and evidence.

## 9. Completion and convergence

The program is complete only when:

- Every audit finding is `RESOLVED`, `BLOCKED-EXTERNAL`, or approved `BLOCKED-POLICY`.
- A fresh full audit reports zero new findings.
- All Critical/High findings are resolved or explicitly blocked with valid governance evidence.
- Local, semantic, team, and full-stack documentation accurately matches each profile's proven state.
- The full verification ladder passes on the final merged commit.
- The final audit delta, live actions still required, rollback notes, and measured before/after metrics are recorded.

## 10. Launch prompt

```text
/goal Execute `.planning/REMEDIATION-OPTIMIZATION-PLAN-2026-07-10.md` autonomously from Phase 0 through audit-loop convergence.

Preserve all existing user changes and begin by proving an isolated execution environment: inspect the dirty worktree, create a dedicated remediation branch/worktree plus venv if safe, and verify `memorymaster.__file__` resolves to that checkout. If isolation would risk user work, remain in the main checkout and stage only explicitly owned files.

Follow the plan in dependency order. Query MemoryMaster before architectural decisions. Its access telemetry and narrowly scoped governance claim ingests are authorized; cycles, cleanup, migrations, compaction, redaction, and backlog operations against the live DB are not. Before editing any symbol, check GitNexus freshness, run upstream impact analysis, and warn me before HIGH/CRITICAL blast-radius changes. Add failing adversarial tests before every security/integrity fix. Use one conventional atomic commit per work package, run `gitnexus_detect_changes()` before commits, then refresh the index with `npx gitnexus analyze --embeddings` when embeddings exist. Keep the branch releasable after every package.

Use temporary databases and local/fake services by default. Run ML-marked retrieval tests when their phases require them; if external models/services make that impossible, create a `BLOCKED-EXTERNAL` ledger item rather than claiming verification. Do not push, publish, deploy, rotate external credentials, or perform product-data mutations on the live MemoryMaster database without explicit approval; document those items in `external-actions-required.md` and continue with all unblocked work.

Do not stop after implementation. Run the plan's full verification ladder, rerun the complete audit against the same scope, reconcile every ledger checkbox against commit/runtime evidence, produce the audit delta, and continue fixing new findings until convergence: every finding resolved or validly blocked, and zero new findings in the latest audit. Give concise progress updates and surface material uncertainty early.
```
