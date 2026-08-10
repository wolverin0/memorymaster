<!-- doc-head: repaired Hermes P5 observer armed -->
# Tencent-derived scope, governed-skills, and Hermes integration specification
# Covers: session binding, native Hermes memory, governed skill proposals, and progressive approved-skill reuse.
# Key terms: TencentDB Agent Memory, SessionBinding, Hermes, durable outbox, skill candidate, approved skills.
# Read when: implementing, reviewing, activating, or rolling back the post-v4.6 companion-integration program.
# Authority & Safety: implements `ROADMAP.md`; Windows SQLite is authoritative, VM is fallback-only, and skills require approval.
# Status: P1-P5 and the graph repair are active; a headless 24-hour observer is armed for the PR gate.
<!-- /doc-head -->

## 1. Outcome

Deliver four bounded improvements without importing TencentDB Agent Memory or
changing MemoryMaster's governed-claims architecture:

1. Bind every agent session to an explicit, visible personal or project scope.
2. Integrate Hermes through its supported standalone `MemoryProvider` API.
3. Convert recurring, reusable workflows into evidence-linked skill candidates
   that cannot become active without an explicit approval.
4. Reuse matched confirmed skills progressively in Hermes recall without
   exposing candidates, stale versions, or another project's instructions.

The release remains personal-first and SQLite-only. It does not add a database
server, team tenancy, a second authority, or automatic global instructions.

## 2. Fixed architecture decisions

- The Windows MemoryMaster SQLite database remains authoritative.
- Hermes runs on the Ubuntu VM and uses authenticated MCP/HTTP for authoritative
  `remember` and `recall` operations.
- Hermes must never open the Windows SQLite database over SMB.
- The VM MemoryMaster database remains a read-only recall fallback. It is not an
  alternate writer while the Windows authority is unavailable.
- Offline Hermes writes enter a bounded durable outbox and replay through the
  authoritative public facade when connectivity returns.
- Existing claim/citation delta synchronization remains a fallback-cache feed.
  It is not extended into a second universal-capture replication protocol.
- Trusted recall remains confirmed-only. Candidate inclusion stays explicit.
- Skills are governed claims with evidence links and supersession history, not
  files written directly by an LLM.
- Per-turn skill reuse is an opt-in recall projection over confirmed scoped
  claims. It is not a second skill store or an automatic activation path.
- Generated `SKILL.md` files first land in a MemoryMaster staging directory.
  Activation under operator-owned global agent directories is a separate,
  previewed operator action.

## 3. Target data flow

```text
Hermes completed turn
  -> SessionBinding resolver
  -> bounded local outbox
  -> authenticated MemoryMaster MCP/HTTP
  -> source item
  -> evidence item
  -> capture job
  -> candidate claim + claim_evidence_link
  -> steward/operator review
  -> confirmed claim
  -> trusted recall with citation

Authority unavailable
  -> recall may use the VM replica and reports degraded=true
  -> writes stay queued locally and never mutate the fallback replica
```

## 4. Scope contract

### 4.1 Resolution order

1. An explicit scope authorized for the caller.
2. An unexpired binding for the current external session.
3. A verified workspace mapped to `project:<canonical-slug>`.
4. `user`.

`global` is never inferred. A global binding requires an explicit request and
an explicit capability that is disabled in the Hermes production profile.

### 4.2 SessionBinding

The immutable public/internal contract carries:

- hashed external session identifier;
- `source_agent` and platform;
- effective scope and canonical workspace slug;
- optional task label;
- binding source: `explicit`, `verified_workspace`, or `default_user`;
- creation, last-seen, expiry, and end timestamps.

Raw filesystem paths and raw messaging-platform user identifiers are not
persisted in the binding table.

### 4.3 Surfaces

- Advanced CLI: `memorymaster session-scope show|bind|clear`.
- MCP: bounded show/bind/clear tools with the same authorization checks.
- Hermes: one `memorymaster_scope` tool for the current session.
- Public receipts: additive `scope` and `scope_source` fields.
- Recall injection: always displays effective scope and trust mode.
- Dashboard: active bindings and their non-sensitive health metadata.

### 4.4 Legacy hook correction

The repository hook template must replace its no-CWD `global` fallback with the
shared resolver's `user` fallback. The installed `~/.claude/` hook is
operator-owned: produce a proposed diff and verification evidence; do not edit
it automatically.

## 5. Native Hermes provider

### 5.1 Packaging

Ship a standalone provider package under `integrations/hermes-memorymaster/`.
It implements Hermes's official `MemoryProvider` interface and does not patch
Hermes core. Pin and test the installed Hermes provider ABI before activation.

Required lifecycle behavior:

- `initialize`: load non-secret configuration, create the bounded outbox, and
  record platform/agent/workspace context.
- `prefetch`: return fast, trusted, scope-limited recall with a hard timeout.
- `queue_prefetch`: warm the next-turn cache without blocking the agent.
- `sync_turn`: enqueue a completed primary-agent turn and return immediately.
- `on_session_switch`: preserve resume/branch lineage and reset new sessions.
- `on_pre_compress`: flush queued observations before context is discarded.
- `on_session_end`: drain bounded work and queue `improve` once for the scope.
- `on_memory_write`: mirror additions through `remember`; removals create a
  retirement preview and never silently forget.
- `shutdown`: bounded drain, durable residue, and no abandoned leased work.

Cron, subagent, flush, synthetic, and system-only contexts do not automatically
write user memory.

### 5.2 Provider tools

- `memorymaster_recall`
- `memorymaster_remember`
- `memorymaster_scope`
- `memorymaster_forget_preview`

No tool may directly confirm a candidate or apply forgetting without explicit
operator confirmation.

### 5.3 Transport and authentication

- Reuse the existing authenticated streamable MCP/HTTP entrypoint.
- Bind only to the private VM-facing interface configured at activation time.
- Require a bearer secret stored outside repositories and logs.
- Restrict the Windows firewall rule to the VM/private interface.
- Grant `user` and verified `project:*` behavior without granting `global`.
- Reject client-supplied paths; the remote profile accepts logical scopes only.
- Keep `/readyz` plus an authenticated functional `recall` probe in readiness.

### 5.4 Outbox and failure behavior

- Persist a sanitized, versioned envelope before acknowledging `sync_turn`.
- Identity is producer + session hash + turn id + SHA-256 content hash.
- Bound queue item count and bytes; report backpressure instead of dropping.
- Use exponential retry, jitter, a circuit breaker, and actionable error codes.
- A process crash leaves replayable pending rows.
- Authentication or scope failures are permanent/blocked, not infinite retries.
- Recall failure returns empty/degraded context within its timeout; it never
  blocks the Hermes response loop.

### 5.5 Capture producer changes

Extend the shared producer envelope with sanitized `external_id`, producer
metadata, session hash, and turn id. The authoritative capture service uses
those fields for replay-safe source identity; adapters still own no network I/O.

## 6. Governed skill proposals

### 6.1 Representation

Use ordinary governed claims:

```text
claim_type   = "skill"
predicate    = "applies_when"
status       = "candidate"
object_value = personal-skill-v1 JSON
```

The structured payload contains:

- slug, title, and SHA-256 content identity;
- when to use and when not to use;
- inputs and prerequisites;
- ordered workflow and decision rules;
- expected output and validation;
- pitfalls and recovery guidance;
- supporting rule/claim IDs;
- expected parent claim/version for optimistic concurrency.

Claim citations and `claim_evidence_links` remain the authoritative lineage.

### 6.2 Reviewer policy

The bounded reviewer:

- treats every transcript and source document as untrusted input;
- classifies each candidate as skill, memory, wiki, code knowledge, or
  temporary context;
- requires a recurring trigger, reusable bounded task, executable workflow,
  and validation procedure;
- reads existing confirmed and candidate skills before proposing a create;
- prefers an update or no-op over duplication;
- requires total quality at least 72/100 and no dimension below 12/20;
- uses existing LLM/provider budgets and finite per-cycle item limits;
- records rejected/unknown output in diagnostics rather than coercing it.

Recurring corrections remain eligible only after at least two independent
observations. When deterministic enforcement is possible, the proposal should
recommend a test, lint rule, hook, or guard rather than relying only on prose.

### 6.3 Promotion and versioning

- Skill extraction always creates a candidate.
- Generic automatic validation must not confirm `claim_type=skill`.
- Approval is an audited human action that atomically confirms the new claim.
- An approved update confirms the new version and supersedes the previous one.
- Rejection archives the candidate without deleting its evidence or audit.
- Reprocessing identical evidence cannot create another version.
- Only confirmed, active, authorized skills participate in trusted retrieval.

### 6.4 Projection and activation

Render confirmed skills deterministically under a MemoryMaster staging root.
The generated header includes claim ID, scope, content hash, version, and
citations. Copying into global Claude/Codex/Hermes skill directories remains a
previewed, operator-gated step outside automatic stewardship.

### 6.5 Progressive per-turn reuse

Public and MCP `recall` accept additive `include_skills` and `skill_limit`
parameters. When enabled, MemoryMaster retrieves confirmed skills under the
same scope allowlist, packs whole workflows into a bounded share of the total
token budget, removes opaque raw skill JSON from ordinary claim context, and
returns both a structured `skills` tuple and an explicit `APPROVED SKILLS`
section. Non-text formats fail closed when skill projection is requested.

Hermes opts into this projection for authoritative MCP recall and read-only
replica recall. Candidate, stale, conflicted, superseded, archived, sensitive,
or cross-scope skills never participate. Ordinary public recall remains
unchanged by default.

## 7. Implementation packages

### P0 - Read-only topology and baseline

- [x] Work only from a clean worktree based on `origin/main`.
- [x] Record current Hermes version/commit and provider ABI.
- [ ] Record `hermes memory status` and active provider without changing it.
- [ ] Verify gateway platform state, not only `/health`.
- [ ] Record Windows/VM DB quick-check, claim counts, and sync-task results.
- [ ] Measure baseline recall latency, sync freshness, and duplicate counts.
- [ ] Create and restore-test snapshots of the Windows authority and VM replica.

P0 live evidence on 2026-08-07:

- The Windows authority was snapshotted to the configured `E:` backup root and
  restored independently to both `E:` and a fast local disposable path. The
  local restore passed `PRAGMA quick_check`, zero foreign-key violations,
  idempotent migration, schema-version, trusted-recall, and prior-runtime
  compatibility checks. The snapshot and both restores have identical byte
  sizes and authoritative table counts.
- Trusted restored recall returned only confirmed claims with citations and
  completed within 9.5 seconds. Replay, scope, and duplicate invariants pass in
  the focused integration gate; Windows scheduled tasks reported result `0`.
- The available WSL Ubuntu instance is not the Hermes host: it has no Hermes
  binary, Hermes home, gateway service, or replica. Known SSH endpoints, keys,
  Docker contexts, and expected service ports did not locate another reachable
  host. Hyper-V inventory is unavailable to the current non-administrator
  account. Therefore the three mixed Windows/VM checkboxes remain open rather
  than inferring VM evidence from the wrong machine.

Exit: a redacted baseline identifies exact install paths, service actions,
rollback commands, and no production state was mutated.

### P1 - Session binding and scope hardening

- [x] Add the immutable binding model, repository, migration, and resolver.
- [x] Wire public facade, CLI, MCP, dashboard, and hook templates.
- [x] Add global-scope negative tests and session switch/resume tests.
- [x] Produce, but do not apply, the operator-owned global-hook diff.

Exit: no implicit-global path remains in the new surfaces or repository
templates; existing public calls stay backward compatible.

P1 verification on 2026-08-07:

- RED was captured as missing `core.session_scope` and `surfaces.session_scope` modules.
- Focused compatibility, migration, authorization, dashboard, and hook gate:
  110 passed; Ruff and `git diff --check` passed.
- New-module coverage: 88% combined; core binding/resolver coverage: 91%.
- Full non-ML run: 4,297 passed, 71 skipped, 97 deselected, 1 xfailed;
  the owned dashboard line-budget failure was fixed and retested. The remaining
  unrelated gate is the workstation's stale installed distribution metadata
  (`3.2.0`) versus the source-tree version (`4.6.0`).
- The installed operator hook was read only. Its hash-pinned proposal is in
  `_intel/briefs/memorymaster-installed-hook-scope-fallback-proposal-2026-08-07.md`.

### P2 - Hermes provider and authoritative transport

- [x] Add the standalone plugin and fake client/backend tests.
- [x] Add replay-safe producer metadata and durable outbox.
- [x] Exercise authenticated MCP/HTTP against a disposable Windows DB.
- [x] Prove offline queue, recovery replay, circuit breaker, and shutdown drain.
- [x] Add headless Windows action templates and VM systemd configuration docs.

Exit: one synthetic VM turn reaches the disposable authority exactly once with
source, evidence, capture job, candidate, and claim-evidence link intact.

P2 verification on 2026-08-07:

- The provider ABI and discovery layout were checked against Hermes commit
  `7cf71c32bbd27ac4044b6b6a5f0c280268e7ecb5`; installation is a previewed
  `$HERMES_HOME/plugins/memorymaster/` shim because that pinned memory loader
  does not consume the general pip entry-point context.
- The focused scope, authorization, HTTP, producer, lifecycle, retry, replica,
  packaging, and capture gate passed: 98 tests; Ruff and `git diff --check`
  passed, and the wheel contains the provider plus installation resources.
- A real authenticated Streamable-HTTP fixture bound and cleared a project
  scope, delivered one sanitized turn, completed its capture job, produced one
  candidate and exact evidence link, previewed retirement, and queued bounded
  improvement work. A wrong bearer token was permanently rejected.
- Enqueue p95 is test-enforced below 50 ms; the real local HTTP path passes at
  the provider's 350 ms default timeout. Five-attempt exhaustion, expired-lease
  recovery, circuit opening, restart replay, bounded shutdown, and read-only
  replica byte integrity are covered.
- No live Hermes profile, scheduled task, firewall, authority database, or VM
  replica was changed. One accidental ignored DB inside the isolated worktree
  was verified as test-only and removed.

### P3 - Skill reviewer and approval flow

- [x] Add `personal-skill-v1` schema, parser, renderer, and validator.
- [x] Reuse rule evidence and correction counts for bounded proposal input.
- [x] Add read-before-write matching and SHA-256 idempotency.
- [x] Block automatic skill promotion and add explicit audited approval.
- [x] Add confirmed-skill recall and deterministic staging export.

Exit: repeated fixture evidence creates one candidate; it is absent from
trusted recall until approval; an approved update supersedes rather than
rewrites the prior version.

P3 verification on 2026-08-07:

- A strict `personal-skill-v1` boundary validates bounded workflow fields and
  five quality dimensions, rejects unknown output, and computes a canonical
  SHA-256 over executable content rather than mutable lineage metadata.
- Recurring rule inputs require at least two observations. Candidate creation
  copies exact evidence links, is replay-safe, and reads existing skill
  versions before creating or updating.
- The generic validator leaves skill candidates pending. Local steward approval
  confirms a candidate and atomically supersedes its immutable parent; version
  races roll back both sides and event-chain integrity remains clean.
- The default-off reviewer uses the configured provider inside the existing
  cycle budget, processes at most 20 items, treats evidence as untrusted, never
  chooses `global`, and records permanent versus retryable diagnostics.
- CLI/MCP operations cover inputs, proposal, review, confirmed-only recall, and
  staging export. Generated files stay under the MemoryMaster staging root.
- Focused skill, rule, validator, lifecycle, MCP, and public-contract gate:
  140 passed; touched-file Ruff and `git diff --check` passed.

### P4 - Convergence, activation, and PR

- [x] Run focused security, scope, replay, lifecycle, and provider tests.
- [x] Run Ruff, collection, migration/restore, and package-content checks.
- [x] Run the full non-ML suite once at the integration boundary.
- [x] Run LongMemEval once; R@5 and MRR may regress by at most 0.01 absolute.
- [x] Run disposable end-to-end activation with fake/local providers.
- [x] Install live provider in read-only shadow mode.
- [x] Enable candidate writes only after all prior gates pass.
- [ ] Observe 24 hours and record scope, lineage, duplicates, outbox, blocked
      jobs, provider state, latency, and task results.
- [ ] Create the PR after the 24-hour check.
- [x] Do not publish a new public release in this package.

P4 verification on 2026-08-07:

- Full non-ML: 4,358 passed, 71 skipped, 97 deselected, 1 xfailed. Focused
  final security/scope/replay/lifecycle/Hermes/capture gate: 155 passed.
  Required ML/retrieval: 97 passed. Collection: 4,527 tests.
- LongMemEval completed once for all 500 questions: R@5 `0.972`, R@10
  `0.984`, and MRR `0.907565`, exactly matching the controlled baseline with
  zero provider calls. It was not rerun after deployment-only fixes.
- SQLite snapshot/restore, repeated migration, quick-check, foreign keys,
  installed-prior-version recall, wheel builds, clean install, package content,
  dependency audit, Gitleaks, Ruff, and diff checks passed. PostgreSQL remains
  explicitly waived for this SQLite-only rollout.
- Final wheels are `memorymaster-4.6.0` SHA-256
  `B74CA275126B51584D2E74694AC0C063220B5058047C269D6AFEE0A03405A733`
  and `hermes-memorymaster-0.1.0` SHA-256
  `8D169DA9BCA0F98FBEBE5756046FEFDC44FA4BD559D200A44F3257C6D65FF121`.
- Dreaming and Steward now point to the verified inactive-to-live runtime using
  `pythonw.exe`. A manual Dreaming task execution returned `0`, queued graph
  work completed once, capture coverage is `ok`, and candidate mode remains
  steward-governed. The previous runtime path is recorded in the audit delta
  for one-command action rollback.
- The actual UbuntuVM was reached with its existing SSH key. Hermes `0.19.0`
  now selects the native `memorymaster` provider; the legacy
  `memorymaster-bridge` is disabled, the provider outbox is empty, and the
  authoritative transport, read-only fallback, Codex OAuth, and Telegram TLS
  paths are healthy. A fixed replay produced one source, one evidence item,
  one candidate, and one exact support link under `project:memorymaster`; the
  identical replay produced no duplicate, and trusted recall returned only
  confirmed claims.
- Live remote recall completed five shadow queries with median `0.492 s` and
  p95/max `1.373 s`. That I/O runs in the provider's background prefetch path:
  the response-loop prefetch return p95 measured `0.032 ms`, while durable
  `sync_turn` enqueue p95 measured `1.032 ms`. The configured three-second
  authority timeout therefore does not block the Hermes response loop.
- The 2026-08-07 observation is retained only as incident evidence. Its VM
  OOM/gateway interruption and absence of P5 make it invalid as a PR gate. The
  2026-08-09 check also failed closed on the graph-identity defect; the repaired
  replacement check is scheduled for 2026-08-10 21:20 Argentina time. No public
  release was created.

Exit: the PR contains reproducible evidence, activation and rollback commands,
and no unresolved scope or lineage invariant.

### P5 - Progressive governed skill reuse (Tencent v2.0 delta)

- [x] Add optional approved-skill projection to public and MCP recall.
- [x] Share one token budget between ordinary claims and complete skill assets.
- [x] Keep raw skill JSON out of ordinary claim context when projection is on.
- [x] Opt authoritative and read-only Hermes recall into the same bundle logic.
- [x] Prove candidate, stale, and wrong-scope skills are never injected.
- [x] Build and activate updated packages through the rollback-safe path.
- [x] Start a fresh 24-hour observation from a clean post-P5 baseline.
- [x] Remediate the 2026-08-09 fail-closed graph-coverage finding without
      weakening coverage or replay safety.
- [ ] Pass a replacement fresh 24-hour observation before PR creation.

P5 local verification on 2026-08-08:

- RED first showed the missing `include_skills` public contract and missing
  Hermes skill section. The initial real HTTP test also exposed cold hybrid
  retrieval exceeding the provider timeout.
- Hermes now explicitly requests deterministic `legacy` retrieval while the
  public default remains `hybrid`. The real authenticated MCP HTTP path and
  read-only replica both return the same confirmed-skill bundle.
- The focused public/Hermes/governed-skills regression set passes: 62 tests.
  It covers candidate, stale, cross-scope, and confirmed states; structured
  skill receipts; bounded tokens; authoritative transport; replica DB-byte
  preservation; and existing skill lifecycle/MCP behavior.
- Full non-ML convergence passes: 4,367 passed, 71 skipped, 97 deselected,
  and 1 expected xfail in 684.58 seconds.
- Isolated wheel builds and a clean dependency-resolving install pass with
  `pip check`. The wheels contain the new bundle/public/MCP/backend modules:
  MemoryMaster SHA-256 `33D6D702DF59EDA0239CA688D3B2BF8B9C6D2101EBE8C78A76D6B24B2A8309D6`;
  Hermes provider SHA-256 `4694023420364BCA4BEBD200ABF2D6328426671FD432F135067F4CE2234EEED9`.
- The earlier live observation is not a clean PR gate because the VM suffered
  an OOM/gateway interruption during its window. P5 activation must be followed
  by a new uninterrupted observation.

P5 live activation on 2026-08-08:

- A new online SQLite snapshot passed `quick_check` and foreign-key validation
  before activation. P5 adds no schema migration, and the prior disposable
  restore/rollback-compatibility gate remains applicable.
- The audited wheels were installed into a side-by-side Windows runtime with a
  clean `pip check`. Dreaming, Steward, and Hermes HTTP now use that runtime's
  consoleless `pythonw.exe`; the prior runtime is preserved for one-action
  rollback.
- The same wheels and exact rollback wheels were installed in the Hermes venv
  with `uv pip --no-deps --reinstall`. The gateway is active with zero restarts
  after the P5 start, `ManagedOOMPreference=avoid`, established TLS sockets,
  and a clean dependency check.
- Five real VM-to-authority recalls were nonempty with median `0.115 s` and
  max `1.045 s`. The provider outbox contains five completed deliveries and
  zero pending, leased, retryable, blocked, or failed deliveries.
- Production has zero `claim_type=skill` claims, so absence of
  `APPROVED SKILLS` in live recall is the correct governed result. An
  installed-wheel disposable canary
  separately proves candidate exclusion, confirmed inclusion, scope isolation,
  and the shared token ceiling without creating production skill content.
- A steward transition confirmed the rollout canary after the previous worker
  cycle. Public `improve` queued its single due graph job without promoting or
  rewriting any claim; the 02:11 hourly worker completed it with result `0`.
  Capture coverage is now `ok` with seven completed jobs and no missing graph,
  expired lease, retryable, blocked, partial, or orphan anomaly.
- The repaired post-P5 observer is scheduled for 2026-08-10 21:20 Argentina
  time. It may push and create the PR only after every longitudinal gate passes;
  it may not tag, publish, deploy, or merge.

P5 graph-queue remediation on 2026-08-09:

- The missing job belonged to a confirmed claim whose graph extraction had
  already completed; later confidence-only validation changed `updated_at` and
  produced a false new graph revision.
- Graph replay identity now uses the latest actual transition into `confirmed`.
  The hourly Dreaming action queues due capture and graph work before leasing it.
- Live repair queued and completed exactly one graph job with zero worker errors;
  authoritative coverage returned `ok`, with no active capture jobs remaining.
- Focused scheduler/capture/graph gate: 46 passed; full non-ML gate: 4,422
  passed, 71 skipped, 97 deselected, one expected xfail; Ruff passed.
- A fresh local SQLite backup and byte-identical disposable restore passed with
  matching schema/lineage/queue state and zero relevant orphans. The attempted
  E-drive snapshot failed a CRC read and is explicitly quarantined as invalid;
  the older verified pre-P5 E-drive snapshot remains intact.
- Clean repair wheel SHA-256
  `06f78585d2b470e273748851fc1c4df5b912de52186743306af87e3042648d0f`
  is installed in the side-by-side Windows P5 runtime with `pip check` clean.
  A real consoleless Dreaming execution returned `0` and logged the bounded
  queue -> capture -> dream sequence. The replacement observer preserves its
  interactive principal, uses `pythonw.exe`, is hidden/start-when-available,
  and has a verified 2026-08-10 21:20 ART start boundary.

## 8. Acceptance gates

### Scope and authorization

- Zero implicit `global` writes.
- Zero unauthorized-scope recall results.
- Telegram/general personal chat defaults to `user`.
- A verified project workspace defaults to its canonical project scope.
- Session resume preserves binding; reset/new clears task-local binding.

### Hermes reliability

- `sync_turn` enqueue p95 below 50 ms.
- Provider recall hard timeout at or below 350 ms for the live injection path.
- Offline writes survive restart and replay exactly once.
- No secret reaches the outbox, logs, source payload, evidence, or claim.
- Authority failure never triggers writes to the fallback replica.
- Queue residue is visible and actionable; no silent drops.

### Capture and lineage

- One accepted turn creates one source identity and one evidence identity.
- Replay duplicate rate is zero.
- Every derived claim has an exact `claim_evidence_link`.
- Candidate creation cannot affect trusted recall before promotion.

### Skills

- Skill candidate precision is at least 90% on the versioned private set.
- Identical input creates no duplicate proposal/version.
- Unknown reviewer output is blocked with diagnostics.
- No skill file is activated automatically.
- Approval and supersession are atomic, audited, and idempotent.
- Per-turn reuse contains only complete confirmed skills authorized for the
  requested scope, stays inside the recall budget, and is off by default.

### Regression

- Existing `remember / recall / forget / improve`, CLI, and MCP contracts pass.
- LongMemEval R@5 and MRR regress by no more than 0.01 absolute.
- One full suite passes at the final integration boundary.

## 9. Activation and rollback

Activation order:

1. Verified snapshots and disposable restores.
2. Authenticated Windows MCP/HTTP action, hidden with durable logging.
3. VM plugin installed but inactive.
4. Read-only shadow recall.
5. Candidate capture enabled.
6. Twenty-four-hour evidence check.
7. PR creation; no public release.

Rollback order:

1. `hermes memory off` or restore the previous provider selection.
2. Disable the MemoryMaster HTTP action and restore its previous task action.
3. Leave the durable outbox intact for diagnosis/replay.
4. Leave additive schema and candidate/audit rows intact.
5. Restore a database snapshot only if an invariant failure affected existing
   rows; ordinary feature rollback does not require database restoration.

## 10. Budget and execution discipline

- Lead implementation: one headless high-reasoning coding session.
- Smaller models may run focused tests or mechanical evidence collection; they
  do not lead scope/auth, lifecycle, or migration changes.
- No subagent fan-out unless the operator explicitly requests it.
- Focused tests after each package; one full suite at the final boundary.
- Maximum two repair iterations per failed package gate before reporting the
  blocker and evidence.
- No continuous GitHub Actions watcher. Check once after push and once at
  completion.
- The 24-hour observation uses scheduled logs and counters, not 24 hours of
  active model execution.

## 11. References

- `ROADMAP.md` - sole product roadmap and release authority.
- `docs/adr/0015-governed-universal-capture-lineage.md` - authoritative
  source-to-evidence-to-claim-to-graph flow.
- `memorymaster/public/v1.py` - governed public verbs and current scope default.
- `memorymaster/capture/producers.py` - current producer normalization contract.
- `memorymaster/bridges/delta_sync.py` - claim/citation-only fallback delta.
- `memorymaster/surfaces/mcp_http.py` - authenticated streamable MCP/HTTP.
- `memorymaster/knowledge/rule_miner.py` - candidate-only recurring rule mining.
- Hermes provider API:
  https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/memory-provider-plugin.md
- TencentDB Agent Memory v2.0 delta reviewed at
  `fe3230f176f1bf5832fee79d12494bbc2d19a8aa`:
  https://github.com/TencentCloud/TencentDB-Agent-Memory/tree/fe3230f176f1bf5832fee79d12494bbc2d19a8aa
- Tencent skill-review prior art:
  https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/fe3230f176f1bf5832fee79d12494bbc2d19a8aa/MemoryCore/src/core/skill/prompts/skill-review-prompt.ts
