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

## Agent routing gate (required before P2-C)

The root coordinator runs on the composer-selected `gpt-5.6-sol` at high
reasoning. Root owns architecture, HIGH/CRITICAL decisions and warnings,
cross-package integration, staging, commits, and final evidence reconciliation.
Child agents never supervise the root or independently declare a package done.

MemoryMaster's project agents are pinned in `.codex/agents/`:

| Agent | Model / effort | Allowed work |
|---|---|---|
| `mm_explorer` | `gpt-5.4-mini` / low | Read-only mapping and inventories |
| `mm_test_runner` | `gpt-5.4-mini` / medium | Focused commands and evidence |
| `mm_docs_ledger` | `gpt-5.4-mini` / medium | Evidence-backed planning/docs only |
| `mm_fast_worker` | `gpt-5.6-luna` / medium | Isolated LOW/MEDIUM implementation |
| `mm_worker` | `gpt-5.6-terra` / high | Bounded multi-step implementation |
| `mm_security_reviewer` | `gpt-5.6-sol` / high | Read-only security/integrity review |

`.codex/config.toml` sets `max_threads = 2` and `max_depth = 1`: at most one
child may run beside root, and children cannot create grandchildren. Delegate
only a concrete bounded task whose parallelism saves time. Every write worker
must receive an isolated worktree and explicit owned files; root inspects and
integrates its diff. Never run concurrent writers in one checkout.

Do not silently promote routine work to Sol. If a pinned model is unavailable,
root either performs the task or records the routing limitation; security,
architecture, and HIGH/CRITICAL decisions must not be downgraded to Mini or
Luna. Before starting P2-C, parse every project TOML file, verify each pinned
model and reasoning level exists locally, and commit this routing gate.

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

P2-B implementation and focused gate completed on 2026-07-13:

- Qdrant exposes bounded ID/hash/score candidates only; stored payloads no
  longer duplicate claim text or provenance.
- Primary-store rehydration rejects orphan, archived, sensitive, wrong-scope,
  wrong-tenant, private, provisional, stale-hash, and malformed candidates.
- Exact reconciliation detects equal-count ID/hash drift, and failed immediate
  writes enter a bounded metadata-only replay outbox.
- Semantic reads remain default-off behind
  `MEMORYMASTER_QDRANT_GOVERNED_READS`; team semantic reads remain denied.
- Focused fake-backed gate: 91 passed; explicit Qdrant ML gate: 38 passed;
  focused new-code coverage recorded 92% for the outbox and 85% for the planner;
  changed-file Ruff and `git diff --check` passed.
- Disposable authenticated/TLS Qdrant runtime parity is `BLOCKED-EXTERNAL` in
  `external-actions-required.md`.

P2-C implementation and focused gate completed on 2026-07-13 from routing-gate
commit `038779f`:

- Scheduled stale/unused archival now enters the canonical lifecycle transition
  authority, preserving optimistic version checks, transition events, status
  timestamps, query-cache invalidation, and replayable Qdrant deletion.
- MCP/context-hook recall opens SQLite query-only, suppresses detail-level and
  token-fanout duplicate retrieval, and emits one sanitized `recall` envelope
  per successful top-level recall while retaining legacy spool replay support.
- Focused P2-C gate: 40 passed in 6.03s; changed-file Ruff and
  `git diff --check` passed.
- Atomic package commit: the conventional `fix: enforce lifecycle authority and
  read-only recall` commit containing this scheduler evidence.

P2-D implementation and focused gate completed on 2026-07-13 from P2-C commit
`732cb684efd48083523bcd86200933cd6e928a7a`:

- The integer-ID entity registry is the canonical relational authority;
  immutable migration `0013` converges legacy graph-first and registry-first
  SQLite stores and declares the matching Postgres schema contract. Optional
  Kuzu state remains a derived projection initialized only by its explicit
  backfill path.
- Relational graph, MCP, recall enrichment, wiki suggestion, export, and Kuzu
  recall opens now validate readiness without lazy DDL. Missing or incompatible
  schema produces an actionable initialization failure.
- Focused P2-D gate: 116 passed in 13.02s; changed-file Ruff passed. An
  independent disposable initialization probe reached schema version 13 with
  all four canonical entity tables and zero foreign-key violations.
- Atomic package commit: the conventional `fix: converge entity schema
  authority` commit containing this scheduler evidence.

P2-E implementation and focused gate completed on 2026-07-13 from P2-D commit
`4fb1725b234a3144299ec66a674ce1b8833fb728`:

- Stop and PreCompact are quiet by default. Verbatim, per-stop distillation,
  correction mining, and checkpoint blocking require explicit
  maximum-capture flags; SessionEnd performs the default distilled ingest.
- A separate WAL-backed capture ledger persists complete-line transcript
  cursors, atomic global/provider/session reservations, effective limits, and
  input/output usage. Default worst-case candidate inflow is bounded to 600/day
  against the 688/day safe intake ceiling.
- Verbatim capture uses appended-only APIs and fails closed when the dry-run
  30-day/512-MiB/75,000-session retention plan is over bounds or its bounded
  scan truncates. Candidate backlog planning is reviewable and read-only, with
  zero automatic transitions.
- The focused gate collected 84 tests: 80 passed in 13.26s and exposed four
  bounded integration issues; the single correction batch passed all 12
  affected/new contracts in 1.88s. Changed-file Ruff and `git diff --check`
  passed. The independent invariant probe confirmed a 600/day maximum
  candidate inflow and the exact retention defaults.
- Atomic package commit: the conventional `fix: bound quiet capture and
  retention` commit containing this scheduler evidence.

P2-F implementation and focused gate completed on 2026-07-13 from P2-E commit
`a1d54773fa5e540ad90b6617e02c983ddbfe3d32`:

- Production media commands now require an explicit, configuration-ready real
  provider. The Atlas contract records the breaking fail-closed change as
  version `2.0.0`; processing/configuration failures return non-zero status.
- Mock, synthetic, placeholder, fake, and fixture providers require both an
  explicit test/development mode and `MEMORYMASTER_ALLOW_SYNTHETIC_MEDIA=1`.
  Synthetic evidence is excluded before deterministic claims, LLM prompts,
  action proposals, citations, and approved-action export.
- Media idempotency is provider-specific, so historical mock evidence cannot
  block later real enrichment. Imported `wacli` evidence remains eligible.
- Focused P2-F gate: 111 passed in 23.30s. Changed-file Ruff and
  `git diff --check` passed; the independent policy/contract invariant probe
  confirmed production fail-closed behavior, explicit test opt-in, required
  providers, and preservation of legitimate imported evidence.
- No paid or production provider was called and no live/external data or
  configuration was mutated.

The next separately authorized package is P2-Z Phase 2 convergence. Do not
begin it from the P2-F goal.
