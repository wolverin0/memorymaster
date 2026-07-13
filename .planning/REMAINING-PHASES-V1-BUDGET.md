# MemoryMaster Remaining Phases — Budget Execution V1

**Created:** 2026-07-12

**Base:** Phase 1 commit `95c2b3a`

**Phase 1 review:** draft PR #185. Do not add Phase 2-4 commits to that branch.

## Objective

Complete Phases 2-4 without recreating the open-ended remediation loop. Each
package gets an isolated branch/worktree, one focused gate, one atomic commit,
and a stop. A full non-ML suite runs once at each phase boundary, not after
individual edits.

Production deployment remains a separate release goal after all phase and
external-action gates pass.

## Shared budget and safety rules

1. Freeze each package to its named findings and acceptance contract.
2. Add tests only for the behavior being changed or a newly reproduced
   Critical/High regression.
3. Use syntax/import checks while editing and one focused package gate before
   commit. Do not run the full suite inside package goals.
4. Run GitNexus impact before existing-symbol edits and change detection before
   commits; preserve embeddings on reindex.
5. Use temporary databases and fake/local services. Never mutate live product
   data or enable a previously quarantined profile without its phase exit gate.
6. Record unavailable models, services, credentials, images, and legal/operator
   decisions as `BLOCKED-EXTERNAL`; continue unblocked work once.
7. No push, merge, release, staging, or production deployment inside package
   goals. Publish one phase draft PR only after its phase convergence gate.
8. Medium/Low findings outside the package become backlog. No broad refactors.

## Phase 2 — Governed core convergence

Phase 2 uses branch `remediation/phase2-governed-core-20260712` and worktree
`G:\tmp\memorymaster-phase2-20260712`.

### Goal P2-A — RetrievalPlanner contract

- Establish one immutable request/planner input and one policy-filtered result
  contract across service, MCP, hooks, CLI, context packing, task briefing, and
  volunteer context.
- Preserve Phase 1 lexical fallback and Qdrant quarantine.
- Do not enable semantic Qdrant reads in this goal.
- Acceptance: same request/policy yields the same authoritative claim IDs and
  trust annotations across covered surfaces.

### Goal P2-B — Governed Qdrant reintegration

- Overfetch candidate IDs only; rehydrate every candidate from the authoritative
  store and reapply the P2-A planner policy.
- Reject orphan, archived, sensitive, wrong-scope, wrong-tenant, stale-hash, and
  malformed points before result construction.
- Add exact ID/content-hash reconciliation and a bounded replayable outbox.
- Keep the semantic profile disabled if disposable authenticated/TLS Qdrant is
  unavailable; fake-backed repository work may complete as blocked external.

### Goal P2-C — Lifecycle authority/read-only recall

- Route scheduled archival through canonical transitions with versions, events,
  optimistic locking, cache invalidation, and replayable vector deletion.
- Make top-level recall read-only and spool at most one aggregated telemetry
  envelope; remove duplicate detail-level retrieval.

### Goal P2-D — Entity schema convergence

- Select one canonical entity registry/graph schema and immutable migrations.
- Remove lazy DDL from read tools and expose explicit graph readiness failures.
- Acceptance covers registry-first, graph-first, backend parity, MCP integration,
  and zero FK violations.

### Goal P2-E — Quiet finite capture and budgets

- Default to session-start recall, on-demand recall, and distilled session-end
  ingest.
- Gate verbatim/per-stop/correction capture behind explicit maximum-capture
  flags; persist transcript cursors, provider/global budgets, and usage.
- Add finite age/bytes/session retention and dry-run backlog controls. Never
  bulk-confirm or mutate the live backlog.

### Goal P2-F — Remove fake evidence defaults

- Require an explicit real media provider in production modes.
- Permit mocks only under conspicuous test/dev configuration and prevent mock
  evidence from creating governed claims/actions.

### Goal P2-Z — Phase 2 convergence

- Integrate P2-A through P2-F in dependency order.
- Run the Phase 2 targeted matrix, full non-ML once, collection, Ruff, required
  ML tests, and disposable backend/runtime gates when available.
- Reconcile only MM-SEC-02, MM-ARCH-01, MM-ARCH-02, MM-LIFE-01, MM-REL-02,
  MM-PRIV-01, MM-COST-01, MM-COST-02, MM-COST-03, and MM-DEMO-01.
- Produce one Phase 2 audit delta and draft PR; stop before Phase 3.

## Phase 3 — Performance and operational readiness

Run separate goals for R3.1 embeddings/reconciliation, R3.2 query/storage,
R3.3 setup profiles, R3.4 service entrypoints/readiness, and R3.5
recovery/observability/privacy. Each package uses focused tests only. The Phase
3 convergence goal runs benchmarks, clean-wheel profile setup, Docker/Helm
smoke, backup restore, and privacy dry-runs once, then produces a Phase 3 delta
and draft PR.

No real backup deletion, privacy erase, deployment, or production-data action
is authorized by this scheduler.

## Phase 4 — Product focus and maintainability

Run separate goals for R4.1 extension boundaries, R4.2 gradual decomposition,
R4.3 governance UX/accessibility, and R4.4 generated release truth. Avoid a
flag-day rewrite; keep compatibility facades and measured size budgets. The
Phase 4 convergence goal runs one full verification boundary, browser/a11y
validation, documentation/release drift checks, and produces the final phase
delta and draft PR.

## Release sequence after Phase 4

1. Merge reviewed phase PRs in order.
2. Resolve every item in `external-actions-required.md` or obtain an explicit,
   documented risk decision from the appropriate owner.
3. Build immutable release artifacts/images and run the final release-candidate
   suite, dependency/history/image scans, SBOM/provenance checks, and disposable
   Postgres/Qdrant parity.
4. Deploy to staging/canary and verify health, MCP handshake, migrations,
   rollback, telemetry, and data-integrity invariants.
5. Request explicit production cutover approval with the exact image/artifact
   digests and rollback command. Production is never inferred from a phase goal.

## Current next action

P2-A implementation and focused gate completed on 2026-07-12:

- Immutable `RetrievalRequest`, `RetrievalPlan`, and `RetrievalResult` govern
  service and MCP recall entrypoints.
- Trusted mode is confirmed-only; exploratory status expansion is explicit.
- Conversational lexical recall uses bounded safe token fan-out while Qdrant
  remains quarantined behind the planner.
- Focused gate: 141 passed, 1 expected xfail; FTS/planner regression slice:
  32 passed; changed-file Ruff and `git diff --check` passed.

Next package is P2-B. Start it only in a separate goal/package; do not combine
governed Qdrant reintegration with P2-A.
