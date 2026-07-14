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

P2-Z Phase 2 convergence completed on 2026-07-13:

- The convergence review found and witnessed a High prompt-recall bypass:
  downstream graph/entity/two-pass streams returned allowed, candidate,
  foreign-scope, and private claims together. One immutable trusted planner
  policy now filters both initial lexical results and every optional stream.
- Capture-ledger and verbatim retention readers now use the canonical SQLite
  connection helpers. P2-C/P2-D compatibility fixtures were converged without
  restoring lazy DDL, candidate trust, invalid entity foreign keys, or
  read-path writes.
- Targeted matrix: 226 passed, 2 failed; exact correction: 3 passed. The single
  full non-ML completion produced 3,964 passed, 28 failed, 10 errors, 70
  skipped, 95 deselected, and 3 xfailed in 915.59s. The bounded correction
  matrix produced 287 passed/12 failed, its remaining cluster 71 passed/1
  failed, and the final exact scope correction passed 2 tests. This is
  compositional evidence; no second full-suite pass is claimed.
- Final boundary: 4,171 tests collected; required Qdrant ML slice 34 passed;
  project/changed-file Ruff and `git diff --check` passed.
- Disposable two-role Postgres and authenticated/TLS Qdrant were unavailable;
  no DSNs, safe opt-in, Qdrant URL, API key, or CA were configured. Their
  existing `BLOCKED-EXTERNAL` entries remain authoritative.
- Audit delta: `.planning/audits/2026-07-13-phase2-budget-delta/audit-delta.md`.
  Draft PR: `.planning/audits/2026-07-13-phase2-budget-delta/pr-draft.md`.
- Atomic package commit: the conventional Phase 2 convergence commit
  containing this scheduler evidence.

R3.1 embedding and reconciliation efficiency completed on 2026-07-13:

- Immutable migration 0014 and both baseline schemas persist the exact
  embedding-content hash plus an authority-scoped durable Qdrant sync cursor.
- SQLite/Postgres embedding caches refresh only missing, model-changed, or
  content-changed claims. Candidate refresh uses the provider batch surface;
  a warm retrieval performs one query embed and zero candidate embeds/writes.
- The semantic downgrade probe is reused as the query vector, preserving the
  post-probe safety check without embedding the query twice.
- Qdrant full synchronization now keyset-pages every authoritative eligible
  claim, checkpoints only completed pages, replays the last incomplete page,
  and resets its cursor only after full convergence. The former per-status
  10,000-row truncation is removed.
- RED evidence: the initial efficiency/cursor contract failed 6 tests; the
  service probe separately demonstrated 2 query embeddings instead of 1.
- Focused correction evidence: embedding/read-only slice 42 passed; Qdrant
  sync/reconciliation slice 43 passed with one existing Pydantic warning;
  migration/semantic-downgrade slice 15 passed. Changed-file Ruff and
  `git diff --check` passed. The first combined invocation hit its 244-second
  execution ceiling without a result and is recorded as a timeout, not a pass.
- Authenticated/TLS Qdrant and disposable Postgres runtime parity remain
  `BLOCKED-EXTERNAL`; no production semantic profile was enabled.
- Atomic package commit: the conventional R3.1 commit containing this evidence.

R3.2 query and storage efficiency completed on 2026-07-13:

- Immutable migration 0015 and both baseline schemas add event-type/time and
  event-type/details/time indexes. SQLite EXPLAIN selects the ordered index;
  disposable Postgres runtime evidence remains part of the existing external
  backend gate.
- Recall corpus/alias statistics are generation-keyed instead of cached forever.
  MCP result limits are finite, list claims/events expose opaque keyset cursors,
  and retrieval v2 rows reference the separately serialized claim array rather
  than duplicating full claim payloads.
- A shared WAL-backed usage ledger provides atomic global/provider/actor
  reservations across LLM primary/fallback calls, Gemini embeddings, MCP
  ingest/checkpoint, and explicitly configured core intake quotas. Unlimited
  defaults remain backward compatible.
- The init fast-path stamp now covers legacy ensure-helper source. Helm PVC
  capacity is validated against the finite verbatim/database/artifact/WAL,
  backup, and operator-headroom envelope instead of a fixed 1Gi request.
- RED evidence: the initial focused contract failed collection on the missing
  limit helper and the durable-ledger contract failed on the missing module.
  The first package boundary then found five migration-runner failures because
  migration 0015 assumed a pre-existing events table. The bounded correction
  makes the migration a no-op on schema-less runner fixtures.
- Focused boundary: 265 passed, 45 skipped, 2 expected xfails, 5 failed; exact
  correction: 21 passed. Supporting focused slices: 132 durable quota/provider/
  intake/embedding tests passed; 82 tokenizer/MCP/fast-path tests passed.
  Changed-file Ruff and `git diff --check` passed. Helm CLI was unavailable, so
  chart validation is static at this package boundary and will be retried at
  Phase 3 convergence.
- Atomic package commit: the conventional R3.2 commit containing this evidence.

The next package is R3.3 setup-profile truth. The active roadmap goal authorizes
continuing without a new goal.

R3.3 truthful setup profiles completed on 2026-07-13:

- `memorymaster-setup --profile` now exposes explicit `minimal`, `semantic`,
  `team`, and `full-lab` component contracts. JSON and human results report DB,
  MCP, recall hook, capture hook, provider, steward, vector backend, and
  dashboard evidence as PASS, PARTIAL, or BLOCKED.
- Requested PARTIAL/BLOCKED profiles return nonzero (3/2 respectively); a
  successful local sentinel no longer masks a missing requested component.
  Team verification requires a PostgreSQL DSN and retains the disposable
  two-role external gate.
- MCP status requires registration plus an importable FastMCP runtime; hooks
  require installed packaged files; remote providers are never called merely
  for setup and therefore remain PARTIAL when only credentials are configured.
  Started-but-unreprobed vector/provider services are PARTIAL, not PASS.
- Session-end distillation is shipped as
  `memorymaster.surfaces.session_end_ingest` and the
  `memorymaster-session-end` entrypoint. Hooks and Codex instructions no longer
  depend on the repository-only `scripts/` tree. Installed-wheel setup now
  fails closed when a project-local Compose bundle is absent instead of
  invoking an unrelated default Compose project.
- RED evidence: four profile tests initially failed because no profile DTO,
  aggregation, parser flag, or nonzero semantic result existed. Focused setup,
  detection, hook, packaged-session, and Qdrant-transport boundary: 161 passed
  with one existing Pydantic deprecation warning. Changed-file Ruff and
  `git diff --check` passed.
- Atomic package commit: the conventional R3.3 commit containing this evidence.

The next package is R3.4 service entrypoints and readiness contracts. The
active roadmap goal authorizes continuing without a new goal.

R3.4 service entrypoints and readiness completed on 2026-07-13:

- Stdio MCP remains the private agent-process entrypoint. A distinct
  `memorymaster-mcp-http` entrypoint uses the official stateless streamable-HTTP
  transport, requires a startup bearer token, validates allowed Host patterns,
  leaves only `/healthz` and `/readyz` unauthenticated, and reports DB
  connectivity failures as readiness 503 responses. The MCP dependency floor
  now matches the streamable-HTTP API used by the installed wheel.
- Docker defaults to the authenticated dashboard HTTP profile instead of
  publishing a stdio process. Compose exposes only loopback port 8765, selects
  dashboard or MCP HTTP explicitly, uses real readiness, and bounds CPU/memory
  for MemoryMaster, Qdrant, and Ollama. Helm rejects stdio/unknown service
  profiles, sources dashboard/MCP tokens from existing Secrets, and defines
  liveness, readiness, immutable image, storage, and resource contracts.
- CI now performs stdio initialize/tools-list, builds the image, runs dashboard
  readiness, runs authenticated MCP HTTP initialize/tools-list, and renders
  both Helm profiles. The protocol smoke uses the official MCP client rather
  than treating an open port as proof.
- RED evidence: eight deployment/auth/readiness tests initially failed. Focused
  deployment/dashboard/setup/supply-chain boundary: 97 passed with one existing
  Pydantic deprecation warning. Changed-file Ruff and `git diff --check` pass
  after the one unused-import correction.
- Runtime evidence: local stdio handshake passed; a disposable local HTTP MCP
  returned readiness 200 and completed initialize/tools-list; the built
  `memorymaster:r34-local` image returned dashboard readiness 200 and MCP HTTP
  readiness 200 with a successful bearer-authenticated handshake. Compose
  rendered successfully with synthetic required inputs. Temporary containers
  and the named test volume were removed.
- Helm CLI and a disposable Kubernetes target were unavailable. The existing
  MM-OPS-02 external runtime row remains `BLOCKED-EXTERNAL`; no Helm runtime
  pass is claimed. Approved Qdrant/Ollama images, TLS runtime, and image scans
  also remain external.
- Atomic package commit: the conventional R3.4 commit containing this evidence.

The next package is R3.5 recovery, observability, and privacy. The active
roadmap goal authorizes continuing without a new goal.

R3.5 recovery, observability, privacy planning, and lease recovery completed
on 2026-07-13:

- Media retries now use bounded owner/expiry leases on SQLite and Postgres.
  Expired work is atomically returned to pending with an audit event; concurrent
  claimers cannot receive the same row. Immutable migration 0016 and both
  baseline schemas carry the lease contract.
- `memorymaster-ops` creates authenticated encrypted SQLite online backups,
  records plaintext/ciphertext checksums and RPO/RTO metadata, and restores only
  into a disposable drill target with integrity and foreign-key validation.
  PostgreSQL and real off-device recovery remain explicit external evidence.
- Operational health evaluates backup age, retry backlog, SQLite integrity,
  WAL/disk pressure, provider failures, and optional telemetry readiness. It can
  persist one aggregate audit envelope with an owner and runbook instead of
  leaving the evidence process-local.
- Privacy planning is read-only and principal/scope/tenant selective. It
  inventories authoritative rows, citations/events, verbatim, artifacts,
  caches, backups, and vector/backend copies; ambiguous tenants and unavailable
  Qdrant/Postgres attribution fail closed. No erase/export/retention mutation is
  implemented or implied.
- RED lease evidence initially failed three tests on the absent lease API; the
  recovery/privacy/operations modules initially failed collection while absent.
  The focused package gate produced 257 passed, 40 skipped, and two failures in
  legacy Postgres test doubles. The bounded correction passed the exact two
  regressions plus recovery/CLI coverage (7 passed). Changed-file Ruff and
  `git diff --check` passed.
- MM-REL-03 and the repository portion of MM-OBS-01 are resolved. MM-OPS-05,
  MM-PRIV-01, and MM-PRIV-02 retain explicit external evidence requirements;
  no live data, backup, provider, or external system was mutated.
- Atomic package commit: the conventional R3.5 commit containing this evidence.

The next boundary is Phase 3 convergence. The active roadmap goal authorizes
continuing without a new goal.

Phase 3 convergence completed on 2026-07-14:

- Collection discovered 4,218 tests. The single full non-ML invocation
  completed with 4,042 passed, 70 skipped, 95 deselected, one expected xfail,
  and ten failures in 942.30s. Nine failures were stale trusted-recall/vector
  test doubles and one was R3.5's direct SQLite open; the one bounded correction
  batch passed the exact combined scope (19 tests). No second full-suite pass is
  claimed.
- The Phase 3 targeted matrix passed 193 tests with 40 marker deselections.
  Project Ruff and `git diff --check` passed. The isolated ML gate produced 93
  passed and two stale candidate/scope fixture failures; canonical confirmation
  and explicit in-scope setup passed the exact two tests. This is compositional
  ML evidence, not a rerun claim.
- The initial three-attempt quick SLO gate exposed an environment-dependent
  benchmark: optional sentence-transformers reduced query throughput to
  1.36-1.46 ops/s even though CI installs no ML extra. The core smoke now pins
  deterministic hash embeddings while the dedicated ML gate owns semantic
  evidence. The corrected gate passed in 6.734s: ingest 52.77 ops/s, query
  20.65 ops/s, query p95 0.0547s, zero misses.
- A clean wheel built and installed into an isolated venv. Minimal setup passed
  after an isolated Codex client root was present; semantic, team, and full-lab
  returned the expected nonzero BLOCKED status without external services.
- Disposable SQLite encrypted backup/restore passed authentication, checksums,
  integrity, zero foreign-key violations, and RTO (0.046s versus 1,800s). The
  operational health result was OK. The tenant-aware privacy plan was dry-run,
  incomplete by design for Qdrant, and reported zero mutations.
- The freshly built `memorymaster:phase3-local` image
  (`sha256:42fa9252110d8b5458c1bf099bb6f9b158b49013622efce06cc88af661b8d146`)
  returned dashboard and MCP HTTP readiness 200; stdio and authenticated HTTP
  initialize/tools-list handshakes passed. Compose rendered with synthetic
  secrets/digests. Test containers and volumes were removed.
- Helm/disposable Kubernetes, approved immutable release images, authenticated
  TLS Qdrant, disposable two-role Postgres, off-device recovery, and an
  organization telemetry backend remain `BLOCKED-EXTERNAL`. No pass is claimed
  for them.
- Same-scope audit found no new unresolved reproducible Critical/High branch
  regression. Scheduled repository findings are resolved; MM-OPS-04,
  MM-OPS-05, MM-PRIV-01, and MM-PRIV-02 remain explicit external blockers.
- Audit delta: `.planning/audits/2026-07-14-phase3-budget-delta/audit-delta.md`.
  Draft PR: `.planning/audits/2026-07-14-phase3-budget-delta/pr-draft.md`.
- Atomic convergence commit: the conventional Phase 3 convergence commit
  containing this evidence.

The next package is R4.1 extension boundaries. The active roadmap goal
authorizes continuing without a new goal.

R4.1 core and companion extension boundaries completed on 2026-07-14:

- Claims, lifecycle, citations, policy, recall, stewardship, stores, and
  telemetry remain in the authoritative core. Wiki/Obsidian, Dream/OpenClaw,
  Atlas/media/actions, local search, and specialized integrations are explicit
  companion namespaces composed by surface handlers.
- Import-boundary acceptance pins prevent core, governance, recall, and store
  modules from importing optional Wiki/vault or bridge modules. Importing
  `MemoryService` no longer installs a Wiki side effect; importing the Wiki
  companion explicitly retains lifecycle autopromotion.
- Stewardship consumes Wiki similarity through the narrow read-only
  `WikiSimilarityCorpus` protocol. Existing local-search, transcription, and
  OCR provider protocols remain the supported integration seams.
- The deprecated generic plugin registry and root compatibility shim were
  removed after their documented one-minor window. GitNexus found zero
  production consumers; the only direct consumer was its deleted test suite.
  No arbitrary validator/retrieval/ingestion entry-point boundary was added.
- RED boundary evidence initially failed three of four tests. The focused
  companion/provider/autopromotion gate then passed 87 tests. Changed-file
  Ruff, syntax compilation, and `git diff --check` passed.
- Atomic package commit: the conventional R4.1 commit containing this evidence.

The next package is R4.2 gradual orchestration decomposition. The active
roadmap goal authorizes continuing without a new goal.

R4.2 gradual orchestration decomposition completed on 2026-07-14:

- The public `MemoryService` facade now inherits the real store-backed
  `IntegrationService` implementation for external sources, Atlas/media
  evidence, retry leases, and action proposals. Exact public signatures are
  preserved; ingest, retrieval, cycle, and initialization bodies were not
  changed.
- The manually HIGH facade boundary was limited to the new base class. The
  `get_action_proposal_by_idempotency_key` symbol was left in the facade after
  GitNexus reported a self-referential CRITICAL graph result; its behavior was
  not changed.
- Dashboard claim/event/conflict/review/mobile/action/audit/namespace read
  models now live outside the HTTP handler. Action-status, triage, and operator
  mutations now use explicit command functions; handlers parse, invoke, and
  serialize.
- Ratcheting tests cap `service.py` at 2,450 lines, `dashboard.py` at 1,550,
  `DashboardRequestHandler` at 720, extracted modules at 800, and extracted
  functions at 50. Actual results are 2,205, 1,381, and 691 respectively.
- `docs/compatibility.md` freezes the root-shim inventory, retains it through
  4.5.x, and requires the dated 2026-09-30/v5 major removal gate plus consumer
  evidence. Telemetry/lifecycle, stewardship/ingestion, and retrieval remain
  prioritized gradual ratchets; no placeholder services were added.
- RED architecture evidence initially failed three of four checks. The focused
  package boundary produced 137 passes and one stale monkeypatch-path failure;
  the exact correction plus architecture checks passed five tests. Changed-file
  Ruff, syntax compilation, and `git diff --check` passed.
- Atomic package commit: the conventional R4.2 commit containing this evidence.

The next package is R4.3 governance UX and accessibility. The active roadmap
goal authorizes continuing without a new goal.
