<!-- doc-head: installed operational repairs and verified NAS recovery; acceptance limits -->
# Useful memory, reliable delivery
Covers: supported recall installation, real CI evaluation and source-level Dreaming sampling.
Key terms: Gemini-only, duplicate delivery, missed facts, label provenance, read-only.
Read when: accepting or operating 4.8.9; ROADMAP.md remains the sole roadmap.
Status: September 21 repairs installed; NAS backup restored and scheduler recovered.
<!-- /doc-head -->

## Product changes

1. Supported recall hooks skip only known machine-event prefixes; discussing a
   marker mid-sentence still recalls. Identical context is suppressed for at most
   five minutes, keyed by full session identity and project context. Changed
   context/new session delivers. SessionStart, including compaction/resume, resets.
2. Delivery state contains hashes/time only and is written after flushed output.
   Corrupt/unavailable state degrades to delivery. It is best-effort context
   optimization, not exactly-once concurrent transport or proof an agent read it.
3. Setup preserves unknown/custom recall hooks and emits a `.proposed` file.
   A full-body fingerprint recognizes untouched managed hooks; local edits are
   preserved too. Existing custom look-ahead functionality is not overwritten.
   Windows path substitution uses forward slashes, fixing generated Python
   escape failures witnessed by the installation round-trip.
4. CI's evaluation job uses the existing qrels and disposable public lifecycle;
   failures and missing JUnit artifacts fail the job. The old manual
   `eval_memorymaster.py` remains a legacy tool requiring explicit case files,
   not a working default CI/acceptance proof.
5. Dreaming's existing evaluator adds useful-selection precision/recall and
   missed/unwanted counts. Duplicate IDs invalidate the evaluation. Only explicit
   `label_origin: human` plus boolean `human_accept` counts for the existing
   human gate; AI/synthetic/unknown origins do not become human review.
   This is an offline label contract, not authentication of the label author.
6. The read-only sampler inventories all capture strata and chooses a bounded
   deterministic sample. It emits capture IDs, counts and fingerprints, never
   source/claim text. It creates no ledger and changes no historical claims.

## How to evaluate

Run `python scripts/sample_dreaming.py --help` for a timezone-aware inclusive/
exclusive source window and optional JSON artifact. Default five per stratum,
maximum one hundred; the entire population contributes to the fingerprint.
Run `python scripts/evaluate_dreaming.py labels.jsonl` for labeled decisions.

Each evaluation record must have a unique nonempty record_id, explicit booleans
should_emit/emitted/structured_valid; emitted records also require evidence_exact,
expected_scope/actual_scope and expected_action/actual_action. Human acceptance
needs label_origin=human and a boolean human_accept. Record one expected fact per
source-supported proposition, including useful facts that produced no candidate,
plus unwanted emissions and routine negative controls. “Should emit” must be
judged against the source, not inferred from the output.

The added thresholds are useful_precision >= 0.90 and useful_recall >= 0.85;
existing evidence/scope/action/structured/sample/human gates remain. These are
acceptance targets, not achieved production scores. Useful selection is measured
separately from citation entailment and factual correctness. The evaluator never
promotes a claim or activates a worker.

The earlier 88-action Gemini review is diagnostic AI evidence and cannot supply
human acceptance labels. No new Gemini call or source-text egress was needed here.

## Actual source population

Measured through SQLite mode=ro, query_only and a consistent read transaction,
for 2026-08-29T15:38:34.710902Z through 2026-09-05T15:38:34.710902Z:

- 188 captures: 118 zero-candidate, 10 with candidates/no recorded actions,
  60 with candidates/recorded actions; 15 selected source captures.
- Source-population fingerprint:
  `7961c8cb75207b990210368d830c77b71ad05b6b07d7eb37e1250e9f0ff15a88`.
- Source selection lives in ignored `artifacts/useful-memory/source-sample.json`.
  These are counts, not proof that the 118 captures contain missed useful facts.
  Precision and useful recall remain explicitly unknown until source labeling.
- Captures may be repeated windows from the same session; they are not 188
  independent human conversations. Action counts are not confirmed memories.

## Verification

- Acceptance checklist: [GATES.md](../GATES.md).
- Initial failing witnesses: missing delivery module; nine evaluator failures
  (missing omission metrics, AI labels counted as human, malformed labels);
  installation round-trip exposed Windows escape failure; editing a managed hook
  initially lost customization, then the digest-based recognition fixed it.
- Current focused tests: 32 passed; delivery/evaluation/sampling coverage 94%
  combined (individual modules 91%, 95%, 96%).
- Installation/source-steward/qrels/public-lifecycle integration: 83 passed.
- Ruff passed. Twelve release-truth tests and generated metadata verification passed.
- Wheel built and installed in a clean environment; isolated 4.8.8 imports and
  delivery/evaluation smoke passed without importing the source checkout.

## Scope, status and next decision

This milestone contains no database migration, backend replacement, ranking
change, extra steward agent, scheduler, provider switch, feature activation or
historical curation. Gemini remains the selected deployment provider. The
upstream ideas are patterns, not copied runtime code.

Next: label a bounded sample including zero-output sources, then keep one
retrieval or extraction-rescue experiment only if it improves matched source
outcomes. Persistent quota cooldown requires evidence of repeated cross-cycle
quota failures; more state is not added speculatively. No 24-hour wait is an
implementation prerequisite.

## Deployment evidence - 2026-09-06 UTC

- PR #252 passed all fifteen checks, including the six-platform/Python matrix,
  ML, performance, real evaluation artifacts and deployment smoke. Merged into
  main as `39bacf871b5fd75737597d23f988da4bd62af30b`.
- Built from merged main; wheel SHA-256:
  `F1465722B70BE8E9E36FD1C1BB044D6225A398C8C298A0BEFBF1CA982FC506E6`.
  Installed 4.8.8 with no dependency changes in the general and dedicated
  scheduled runtimes. Prior 4.8.7 wheel and pre-change custom hooks retained.
- Restarted only the managed HTTP MCP task. Fresh installed runtime reports
  health/readiness 200, unauthenticated rejection 401, 51 tools and successful
  authorized read-only recall. Startup was found in the configured log with
  zero error/traceback lines in the post-start tail.
- A separate fresh installed stdio process passed initialize/tool discovery
  with 51 tools using a disposable workspace; no authoritative-row test writes.
- Installed-package demo passed candidate promotion, cited opt-in observation
  recall, ordinary exclusion and automatic staleness after support retirement.
- Reconciled the existing custom recall hook with bounded combined-context
  delivery, preserving task look-ahead. Its duplicate-delivery witness failed
  before reconciliation and passed afterward, including real installed
  SessionStart reset against a missing disposable DB. Hooks apply next event.
- Nine older stdio MCP processes belonging to eight live agent processes were
  left intact. Reconnect MemoryMaster MCP or restart/resume those sessions,
  including this one; a package install cannot reload their imported modules.
  No workstation or terminal-host restart is required. HTTP is already restarted.
- Scripts, disposable test results and hook rollback copies are retained under
  ignored `artifacts/deploy-4.8.8/`. No GitHub release tag or PyPI publication,
  historical curation, provider change, new scheduler or database migration.

## Daily operational review - 2026-09-06

Overall: ATTENTION REQUIRED, not a feature-quality PASS. Real read-only checks
ran approximately 14:23-14:40 UTC. Authoritative database and installed package
are healthy; declared activation still differs from the installed worker.

### Observed runtime and retained state

- Runtime 4.8.8; authoritative SQLite quick_check=ok, zero foreign-key errors,
  migration 24. Trusted canary mm-8aef ranks fourth. Thirty-eight claims checked
  over 24 hours have zero private-context detector matches (not a full raw-source
  privacy audit). HTTP health/readiness 200, unauthorized 401, 51 tools and
  authorized read-only recall pass.
- User-level graph/profile flags are DISABLED (0), while the scheduled launcher
  explicitly sets both to ENABLED (1). Dreaming also has --apply-candidates;
  four recent runs record dry_run=0. This remains inconsistent with the retained
  shadow/off declaration. No activation flag was changed by this review.
- Graph queue: 15,198 completed discoveries, four completed syntheses, one
  cancelled synthesis; no pending/retryable/blocked jobs or expired leases.
  Last 24 hours: 653 no-support and four no-component discoveries; zero new
  syntheses. All three observation claims are archived. There is no current
  observation precision or live promotion sample. 348 support joins have
  explicit sensitivity; no generated-observation edge reinforcement exists.
- Capture queue retains 93 historical blocked jobs: 71 graph_claim_ineligible,
  20 graph_claim_unavailable, one ontology_validation_failed and one
  attempts_exhausted. Last updates are August 11-21, not a new daily failure.
  No expired capture/Dreaming leases. Dream ledger: 185 applied, 339 extracted,
  17 captured rows. No queue was drained or rewritten during inspection.
- Actual 24-hour provider usage: 25 successful Google Gemini 3.5 Flash Lite
  extraction calls and six successful Antigravity Gemini 3.7 Flash Low calls;
  410,116 input plus 7,044 output tokens. No GLM call in this window. Currency
  cost and semantic usefulness remain unmeasured. Selected profile map/reduce
  now use Gemini 3.7 Flash Low; old GLM labels belong to historical runs.
- Latest profile run 3 completed August 30 17:00:26 UTC at watermark 10,184,941;
  latest user message ID is 10,190,314. Seven-day due time is September 6
  17:00:26 UTC, still NOT YET DUE when checked. Scheduled logs corroborate
  not_due. Fifty-two active facts / 565 supports have zero support-hash or
  source-message/session mismatches, and zero expired preferences.
- Projection exactly matches renderer and manifest: 50 selected facts, 1,386
  tokens within effective 1,400-token/60-fact limits. Two stored facts do not fit
  the current rendered projection. Generated marker and installed SessionStart
  loader pass. The old 800-token/40-fact expectation is obsolete. An actual
  post-fix compaction event has not yet been observed; fixture reset passes.
- Steward's latest natural run confirmed two claims, kept four pending, used
  zero provider calls and drained the recall spool to zero. Optional ML
  classifier reports missing joblib/DISABLED; deterministic validation ran.
  Wiki absorption remains DISABLED; neither disabled lane earns quality credit.
- Hermes export contains 6,030 claims, max updated_at matching its 05:24:17 UTC
  watermark; export file updated 07:07 UTC. Remote application/round-trip
  convergence is UNMEASURED, not proven by the Windows task's zero exit.
- Latest NAS backup: memorymaster-20260906T093004Z.db, 7,094,222,848 bytes,
  namespace backups/wolverin0/20260906. Today's scheduled local/remote SHA-256
  match is b2af6418b693ef1e91ebc89188879f11788c082b05a4c48c8ed2268272a07337.
  This review independently opened the NAS copy read-only: quick_check=ok,
  foreign_key_check returned no rows, migration 24. This proves a readable,
  integral backup, not a full restored application rehearsal.

### Bounded repairs and verification

- The installed review incorrectly pinned expected_version=4.8.7. Removed that
  obsolete pin and made the wrapper omit an empty override, allowing the existing
  workspace-version check to apply. Installer default now leaves it unpinned;
  deliberate explicit pins still work. Three regression witnesses failed before
  the fix. The installed wrapper rerun now reports 4.8.8 against pyproject and
  exits 0 after real checks; that narrower PASS is not overall acceptance.
- SessionStart was registered only for startup/resume despite supporting reset
  after compaction. Fixed the live MemoryMaster-only matcher and source installer
  to startup|resume|compact, preserving other hooks. Its installation witness
  failed before the fix and passes afterward. Config/runner backups are retained
  in ignored artifacts. No new wheel or public release was made in this review;
  source installer corrections must be included in the next package.
- Fifty-five focused installation/review tests pass; Ruff and generated release
  metadata checks pass. Installed hook fixture and disposable installed-package
  capture/promotion/cited observation recall/retirement lifecycle pass.
- Primary GitNexus symbol impact lookup raised a cached graph assertion; the
  separate existing worktree index reports installer impact LOW with two direct
  callers. Exact diff and direct-call tests were inspected; no graph-cache
  replacement or shared-process kill was performed.
- Today's checkpoint delivery at 14:25 UTC is transport only. A separate work
  receipt is written after these checks with attention_required and no feature
  success watermark. Checks performed zero database writes; afterward one new
  scoped audit-memory candidate (mm-afbc) was recorded under the project memory
  instructions. No historical claim, queue or source row was modified.

Next decision: reconcile declared shadow/off intent with the already-enabled
worker before declaring feature acceptance. Do not infer authority to turn it
off or on from this review. Evaluate source-level usefulness and actual client
delivery separately; archived observations and a not-due profile cannot supply
new quality evidence.

## Operator-approved activation - 2026-09-06

The operator's subsequent request, "lets enable everything and validate it works",
resolves the activation decision above for the reviewed Dreaming, graph and
profile lanes. Both user-level generation flags changed from 0 to 1. The existing
Dreaming launcher already sets both to 1 and retains --apply-candidates. Existing
Gemini providers, cadence, budgets, candidate governance and opt-in recall remain
unchanged. No retired task, wiki generation, workflow promotion or bulk historical
cleanup was enabled. No package rebuild or new release was necessary for these
runtime settings; the preceding source-installer fixes remain uncommitted.

### Real checks, 14:52-15:00 UTC

- Before/after content-free SQLite inspections are retained in ignored
  artifacts/activation-20260906-before.json and activation-20260906-after.json.
  Today's independently verified NAS backup remains the pre-activation backup;
  no migration or database replacement occurred.
- A bounded, explicitly forced profile step used one real Gemini map call to
  start run 4 before its ordinary weekly due time. The scheduled Dreaming worker
  then resumed it with exactly three map calls, its unchanged per-cycle limit.
  Watermark advanced from 10,184,941 to 10,185,514 toward fixed target 10,190,314.
  Both run model labels are gemini-3.7-flash-low; no error is recorded. This is
  real resumable progress, not a completed new profile. The previous projection
  remains exact: 52 active facts, 50 rendered, 1,386/1,400 tokens, maximum 60 facts,
  no support/source mismatches or expired preferences, generated marker intact.
- Existing Dreaming task completed run dream-7fe916c411a74bf9a24d8b96782704ae:
  four captures extracted/consolidated/applied, zero candidate claim writes,
  zero proposals and zero errors. Its four provider calls were Google Gemini
  3.5 Flash Lite extraction; empty candidate batches required no consolidation
  provider call. Application counts are capture-ledger work, not four new claims.
- Graph discovery completed 164 jobs: 163 no-support and one no-component;
  no production synthesis calls or emitted observations. Queue is empty, no
  expired leases, no generated-observation edge reinforcement. Historical 93
  blocked capture jobs were retained, not mass-retried. The existing three-call
  synthesis batch test passes; zero live calls do not establish a nonzero hourly
  budget enforcement or observation-precision measurement.
- A separate synthetic, no-database-write smoke used the real selected graph
  provider and Gemini profile reducer. Graph returned a valid emit with only
  allowed IDs; reducer returned one valid add decision. The initial reducer
  fixture used an invalid category and failed before any reducer provider call;
  corrected to the schema's identity_locale and passed. Both artifacts retained.
- Installed disposable demo passes candidate promotion, ordinary observation
  exclusion, opt-in cited recall, support-retirement staleness and stale recall
  exclusion. Forty-four focused graph/profile tests pass. These are fixture
  results, not a semantic score on real historical memories.
- Existing steward task ran successfully: five candidates checked, one confirmed,
  four pending; 32 claims became stale through normal decay, 14 recall spool
  entries drained, zero provider calls. No explicit historical remediation was
  performed; normal worker retention and lifecycle writes did occur.
- Actual scheduled operational review (not an environment-overridden shell)
  finished 14:55:59 UTC, exit 0, with graph/profile enabled=2/2, database integrity,
  exact profile support and canary rank 4 passing. Dreaming and steward task exits
  also 0. Installed HTTP health/readiness 200, unauthorized 401, 51 tools and
  authorized recall pass after the worker runs. Installed hook behavior passes.
- Authority decision recorded as new candidate mm-d946, not self-confirmed.
  Work receipt follows real checks; no semantic feature-success watermark is
  advanced. artifacts/activation-20260906-tasks.json records actual schedules.

Remaining: let existing six-hour runs finish profile catch-up; assess real-source
usefulness separately. No eligible live graph component currently supplies a
production synthesis sample. Existing stdio clients predating 4.8.8 still need
reconnection for that release's code; these activation flags do not require
killing sessions, and installed SessionStart reads the projection per event.

## Operational closeout deployed - 2026-09-06

Operator authority: "lo que tengas que hacer aca, HACELO" after asking whether
this pane can close. Scope is finishing the existing installer repairs and local
deployment, not another feature, public release or historical data cleanup.

- PR #253 merged as ca4e4bd28df71d1e50e8ee57161a87cec677f8bc at 19:50 UTC.
  The original ten-file local patch was preserved in a scoped stash before
  fast-forwarding main. Unrelated delta-exchange content was not staged or moved.
- Both general and scheduled runtimes installed the exact 4.8.9 wheel with
  --no-deps and --no-index. SHA-256:
  5D7E97696FB6A159F105216C8743816ADAA9EA30796CA85A42DD9BF85BDD982C.
  The prior 4.8.8 wheel remains available for rollback. No dependencies, schema,
  provider selection or historical rows changed. Local generated egg-info was
  refreshed so checkout metadata cannot shadow the installed version.
- Restarted only the owned MemoryMaster-MCP-HTTP-Hermes task; its listener moved
  from PID 52244 to 82132. Fresh isolated Python processes in both installations
  report 4.8.9, health/readiness 200, unauthorized access 401, 51 tools and
  successful authorized recall. A separate fresh stdio session exposes 51 tools.
- Installed custom hook passes briefing preservation, duplicate suppression,
  machine-event filtering, genuine human-prompt recall and reset. Both installed
  installer code and live SessionStart registration include compact. Live review
  configuration is unpinned; generated release-truth verification passes.
- Fresh HTTP startup log contains the new PID and completed startup with zero
  ERROR/CRITICAL/Traceback lines in the bounded post-startup log segment.
- The existing scheduled operational review was started after deployment at
  19:51 UTC and is still performing its read-only scan at this checkpoint.
  Its older 14:55 UTC PASS artifact belongs to 4.8.8 and is NOT counted as a
  completed 4.8.9 review. The task can finish without this pane and publishes
  its own result/history; deployment verification above is separate evidence.
- Seventy-eight focused installer/review/release tests and scoped Ruff passed.
  PR CI run 34052986088 passed Linux and Windows on Python 3.10/3.11/3.12,
  ML, evaluation, deployment smoke, security and contract/release checks.
  The first performance job failed its ingest thresholds; one targeted rerun
  of identical code passed (ingest p95 0.01546 s, 84.83 operations/s, zero query
  misses). Failed evidence is retained. No thresholds, test assertions or
  benchmark-path source were changed to obtain this pass; runner variability
  remains an inference, not a diagnosed code fix. The earlier main-branch
  Hermes HTTP timeout also passed unchanged locally and in this PR's matrix.
- Primary GitNexus rebuilt successfully with embeddings preserved: 12,262
  embeddings, 15,809 nodes. Initial cached-WAL warnings did not prevent progress
  or completion; no shared MCP process was killed and no cache was deleted.
- Snapshot cleanup is complete: 99 verified test/orphan folders (112 files,
  77,066,240 bytes) were moved to Windows Recycle Bin and remain recoverable.
  Both real August 25/September 1 snapshots and the small retirement audit were
  retained. This was not deletion of authoritative memory or its real backups.

Automatic Dreaming, steward and resumable profile work continue independently
of this pane while the machine/session meets their Windows task conditions.
Agent checkpoint prompts still need an existing target pane; closing it does
not leave an autonomous coding/review agent running. Current profile catch-up
and real-source semantic usefulness remain separate from this deployment proof.
No semantic feature-success watermark is advanced by this closeout.

## Daily operational review — 2026-09-21

**ATTENTION REQUIRED.** ROADMAP and installed configuration were read before
the checks. The authoritative database and auxiliary Dreaming ledger were opened
with `mode=ro` and `query_only=ON`; no live lifecycle cycle, generation, activation,
installation or production repair was executed. Existing shared source changes
were preserved. Evidence is in `artifacts/operational-review/20260921/`.

### Current runtime and observed outcomes

- Both general and scheduled runtimes report 4.8.9. Their installed wheel SHA-256
  is `cddcc637b8f7ce8b665b0e93a2bc94f3aac129f6681ad866c36599a57d45788a`;
  service, skills and Dreaming-provider module hashes agree across runtimes.
  Dirty checkout files are not evidence of installed changes.
- Fresh installed operational review at 14:30:51 UTC: SQLite quick-check OK,
  zero foreign-key errors, migration 25. Overall **FAIL** because the configured
  recall canary `mm-8aef` is absent from top five in **legacy** retrieval. Its
  confirmed fact (128576) still exists, confidence 0.5707; another archived
  heartbeat shares the human ID. Do not retire this failure as a stale expectation
  or replace the canary merely to obtain a pass. Read-only diagnosis finds the
  target in the 60-candidate pool. It ranks fifth before the configured
  three-per-session diversity cap, which removes it. This was isolated by an
  in-memory diagnostic only; production ranking/configuration was unchanged.
  Follow-up must reconcile the canary with the diversity contract, rather than
  widening the pool or silently weakening that guard.
- Installed disposable demo passed capture, cited recall, observation exclusion,
  retirement and temporary-database disposal. Source lifecycle tests passed
  **13/13** (19.50 seconds). These establish lifecycle behavior, not real-source
  semantic usefulness or live ingestion precision.
- MCP HTTP health and database readiness both returned HTTP 200. Task result
  267009 means running, not a failed execution. Authenticated tool round-trip and
  remote-client reachability were not measured by those health probes.
- Capture queue: 210 extraction jobs completed (11 retain
  `partial_provider_output`), 158 graph jobs completed, and **93 blocked**:
  1 attempts exhausted, 71 graph claim ineligible, 20 unavailable, 1 ontology
  validation failure. No jobs were requeued or deleted.

### Activation, provider evidence and retained work

- September 6 approval in ROADMAP remains the activation authority. Installed
  Dreaming launcher explicitly enables graph observations and compiled profile;
  its task includes `--apply-candidates`. User settings agree. Old September 5
  shadow/off expectations are historical, not the current contract.
- Current selected extraction is Gemini via `google`, model
  `gemini-3.5-flash-lite`; consolidation is `antigravity`, model
  `gemini-3.7-flash-low`. In the rolling 24-hour auxiliary ledger sample:
  extraction has 26 structured-valid and 3 structured-invalid HTTP-200 calls;
  consolidation has 10 valid calls and 3 errors with HTTP status 0.
  Historical GLM/OpenCode labels do not describe these calls.
- Latest Dreaming run at 09:11 UTC is **partial**, with 4 extracted, 4 consolidated,
  3 applied, zero candidate writes and 2 errors. Scheduler result 1 agrees with
  partial execution; an `ok: true` envelope does not make it clean success.
  Retained Dreaming states: 204 applied, 18 captured, 345 extracted, 2 retryable.
  The two retryable reasons reject unsupported source/useful selection or
  scope/sensitivity mismatch. These guards were not bypassed.
- Steward selects Google `gemini-3.5-flash-lite`, with no fallback. Latest log
  records zero provider calls, so selection alone is not provider-use evidence.
  Classifier is requested by the installed hook but **DISABLED** at runtime:
  scheduled environment lacks joblib, confirmed by import availability and log.
  Wiki generation remains **DISABLED**. JEV ingest shadow flag is unset;
  the closed T-0504 evaluation is not an active production filter.

### Graph, profile and session delivery

- Graph has 3 observations, 63 observation supports, 173 entity edges and 348
  edge-support rows. Fresh eligibility checks found zero unknown-sensitivity
  supports and zero ineligible confirmed observations. No active/expired graph
  leases; 23,921 completed jobs and one cancelled synthesis job remain retained.
  Latest discovery reaches September 21 but emits no supported components.
  Zero output is an observed outcome, not evidence of synthesis quality.
- Effective profile bounds: weekly cadence, 3 map calls, 500 messages,
  24,000 input characters, 2 independent sessions, 90-day preference TTL,
  1,400 output tokens, 60 facts and reduce batches of 40. Dreaming retains
  40/12 extraction/consolidation calls, 2,000,000 input tokens/day,
  200 candidate writes/day, 18,000 context characters and 900-second leases.
- Profile has 3 completed and 1 cancelled run. Latest completed run 4 has
  watermark 10,190,314 (September 10), with `ProfileValidationError` retained.
  SQLite has 61 active facts; the September 21 projection has 52. All projected
  IDs, timestamps, support counts/hashes and exact support-ID manifests match
  SQLite: **zero mismatches**. This is an exact subset, not all active facts.
  A new rendering timestamp is not a new compilation watermark. The latest
  eligible source ID is also 10,190,314: `no_changes` is consistent with no new
  eligible input, even though weekly cadence has elapsed. Token metadata is not
  present; byte size was not misreported as a measured token count.
- Generated `user.md` has its required marker and is 5,665 bytes, within the
  installed SessionStart 16,000-byte limit. Today's hook log records successful
  injection. This proves delivery execution, not improved agent decisions.
  Retained profile candidates: 108 unconsumed (40 from cancelled run 1 and 68
  from completed run 2). Media retry queue empty; capture leases inactive.

### Follow-up ownership and acceptance limits

No newly introduced, bounded code regression was isolated in this review; no
speculative ranking change, dependency installation or production restart was
used to make checks pass. Recall investigation and classifier dependency/runtime
alignment remain open findings in this ledger. Missing classifier dependencies
were already recorded on September 6; this is an unresolved baseline failure,
not a regression attributed to today's review. Old release-specific 4.7.6
acceptance expectations do not override the verified current 4.8.9 package.
Delivery/checkpoint task success
does not advance a feature-success watermark.

## Weekly acceptance review — 2026-09-21

**NOT ACCEPTED as a feature-quality success.** This extends the daily review
above, using the rolling seven days ending at the evidence timestamps around
14:42 UTC. Counts come from normal WAL-aware read-only SQLite connections.
  Evidence: `weekly-activity.json`, `weekly-lineage.json`, `profile-graph.json`,
`canary-diagnostic.json` and `backup-sync.json` in the same artifact directory.

- Dreaming executed **26 partial, non-dry-run runs**. Actual provider-call rows
  show Google `gemini-3.5-flash-lite`: 186 HTTP-200 calls, of which 157 structured
  valid and 29 invalid; Antigravity `gemini-3.7-flash-low`: 74 valid HTTP-200
  calls and 4 errors with status 0. These are call outcomes, not semantic
  precision. Recorded run model labels happen to agree with this window's call
  records, but are not substituted for them. **Cost UNKNOWN**: token counters
  do not provide an authoritative billed-cost measurement.
- Application ledger records **10 adds and 169 ignores**. All 10 added claims
  exist and have nonempty citation source, locator and excerpt. None has a
  `claim_evidence_links` row; this structural check does not establish semantic
  source entailment. Across the authoritative lifecycle, **40 distinct candidate
  claims were promoted to confirmed**, all with citations, and 911 decayed to
  stale. Promotion and citation presence are not quality labels.
- Queue age is material: retained captured/extracted work reaches July 22;
  extracted rows have up to 9 attempts. Two retryable captures reach September
  11 and have up to **61 attempts**, with source/scope validation errors retained.
  Capture-stage blocked jobs reach August 11 and remain 93. No retry loop was
  manually triggered and no rejected input was silently accepted. The latest
  weekly snapshot has 19 captured rows versus 18 earlier in the daily snapshot;
  independent capture continued during this read-only review.
- Retrieval impact over real tasks is **UNMEASURED**: no matched before/after
  cohort was run in this review. The one live recall canary fails its top-five
  expectation because of session diversity; this cannot establish aggregate
  recall quality. Disposable retirement tests passed, separately from live
  retired-source observation availability.
- Graph discovery completed 3,679 jobs in seven days, with **0 new observations
  and 0 new supports**. Observation precision is **UNMEASURED (n=0)**, never
  100%. All 3 persisted observations are archived. There are zero retired-source
  support rows, so the zero ineligible-confirmed-observation result has no
  positive live sample. Retired-support exclusion is verified by the installed
  disposable lifecycle, not by a fresh production retirement sample.
- Profile active facts: **61/61** meet the independent-session bound (range
  2–18). Of these, 17 are preferences, none expired under the effective 90-day
  TTL, and 44 are stable facts retained as active. New profile supports in the
  window: 0. Rendered facts are 52/60 allowed; measured token count is
  **UNMEASURED**, while configured token limit remains 1,400. Seven-day unsafe
  rejection count/reason distribution is **UNMEASURED**; the old run's aggregate
  `rejected=53` is outside this window and is not a safety-specific numerator.
- Graph jobs in the window have at most one attempt and no open queue. One
  capture job was created/completed in the window with no retry; old retained
  blocked capture and Dreaming queues above are separate from that new activity.
- Wiki generation and JEV ingest shadow remain **DISABLED**, with retained
  queues untouched. No missing samples justify activation. The missing-joblib
  classifier remains an observed runtime failure rather than an intentional
  acceptance pass.

### Sync and backup acceptance

Hermes local AM/PM task results are zero. Both local delta databases pass fresh
read-only quick-check: Hermes 4,895 claims/7,787 citations; Windows 4,136/6,985.
The local watermark is September 21 03:13:58 UTC. Remote merge acknowledgement
and end-to-end round-trip are **UNMEASURED**; local files and task exits prove less.

Weekly NAS backup task failed with `0x800710e0`; September 14 recovery task
returned 1. Latest dedicated remote artifact is
`wolverin0/20260914/memorymaster-20260914T041733Z.db`, 7,167,848,448 bytes.
Its creation receipt reports a matched size/SHA-256, and a current read-only
header probe opens it. That is **partial evidence**, not a fresh full integrity
or restore rehearsal. No backup was launched or restored over production.
The matching local staging DB is absent. The local semantic-candidates snapshot
belongs to deployment/evaluation safety, not the operational NAS-backup namespace;
it is not substituted for a verified current operational backup.

Required follow-up remains in this ledger: reconcile recall-canary expectations
with session diversity; diagnose repeat Dreaming validation failures without
bypassing rejection; align the scheduled classifier dependency; recover backup
scheduling and independently verify a restorable copy. Remote sync acceptance,
new observation precision, unsafe-output rejection effectiveness and real-task
retrieval benefit remain unmeasured. No production repair or activation was
performed without an isolated regression and its acceptance evidence.

## Operational repairs — 2026-09-21, after review

Operator requested execution of the outstanding bounded repairs, not another
diagnostic handoff. Work is isolated on `fix/operational-review-20260921`, based
on 7955291, preserving the shared checkout and installed semantic-candidate/JEV
code. Package comparison against the installed wheel confirmed baseline parity.

- **Recall cause corrected:** the earlier report identified the diversity cap
  but stopped before checking its key. `_source_session_key` grouped all claims
  from a generic agent such as `claude-session`, even when citation lineage
  identified different sessions. The fix prefers a single explicit session
  citation; ambiguous/missing session provenance preserves the prior fallback.
  The cap remains three. Session locators are hashed before trace emission.
  Regression failed before the change; 24 recall/review tests pass afterward.
  Read-only source-runtime canary now returns the unchanged target at rank 5.
  This corrects the monitor's underlying recall behavior, not its expectation.
- **Dreaming retry cause corrected in source:** consolidation source-review
  validation errors bypassed the existing semantic-attempt limit and always
  became retryable. The shared bounded failure disposition now covers extraction,
  consolidation and application replay; a capture failed in one batch cannot
  consume another provider call in a later batch of the same run. Repeated
  invalid output is quarantined, never accepted. Existing capture rows were not
  manually changed or requeued. Focused Dreaming tests and Ruff pass.
- **Configured classifier runtime repaired:** the installed steward hook already
  requested v3, but its environment lacked the required dependencies. Installed
  only the same five pinned versions verified in the working general runtime:
  joblib 1.5.1, scikit-learn 1.7.1, numpy 1.26.4, scipy 1.17.1 and threadpoolctl
  3.6.0. Scheduled runtime passes dependency checks, loads the unchanged v3
  artifact (22 features) and makes a finite synthetic prediction. Four
  classifier fallback/rollback tests pass. No live promotion cycle was invoked.

Evidence: `recall-repair-source.json`, `classifier-repair.json`,
`repair-tests.json`; the old wheel is preserved with its verified hash under
the review artifact's `rollback/` directory.

Independent review blocked the first Dreaming patch: generic `attempts` includes
successful transitions, and provider-side review validation can reject a mixed
batch before worker-level isolation. Neither rejected candidate was installed.
The final fix counts consecutive semantic failures per stage in existing durable
error metadata, preserves that metadata for cached application replay, and keeps
provider batches within one capture. First semantic failure retries; repeated
failure reaches quarantine. Transient errors reset the consecutive count.
No schema migration is needed. Per-capture isolation can defer more sessions
under the unchanged daily call/token budgets; those limits were not raised.
The final focused Dreaming suite passes 81 tests, and the independent reviewer
passes 79 targeted tests with no remaining deployment blocker. The first full
non-ML run was interrupted after the review finding; its log remains evidence
of an incomplete run, not a PASS. The final full non-ML gate passed 5,169 tests, with 75 skipped, 90 deselected,
1 expected failure and native exit 0 (1,378.14 seconds).

### Recovery evidence and remaining NAS constraint

- Fresh local operational backup completed September 21 at 17:48:58 UTC,
  7,585,837,056 bytes. SQLite's online backup API read the live WAL consistently;
  a second isolated restored file has the same SHA-256:
  `d7844db4b19e07e5bcd42c80e1a8e462e35ceab7d20a47c4a4b347c790d08d1c`.
  Both pass quick-check, zero FK errors and migration 25; authoritative table
  counts match, and restored trusted recall returns five cited confirmed claims.
  Neither the live database nor old backups were overwritten. Evidence:
  `local-restore-proof.json`; local namespace `operational-repair-20260921-b1b824fde9`
  under the existing per-user MemoryMaster backups directory.
- The September 14 remote backup now also passes a fresh full hash comparison,
  SQLite quick-check and FK check. This supersedes the earlier unmeasured
  integrity result; it is still an older remote copy, not today's local snapshot.
- Added and tested `MM_PRESERVE_ALL=1` in the existing infra backup script so a
  recovery verification does not clean aged staging files or remote retention.
  The adversarial preservation test failed before the repair and passes after.
- A new NAS upload was **NOT_STARTED**: its coordinator admits MemoryMaster only
  Sunday 01:00–04:00 local time. Today is Monday; its old recovery exception has
  expired. The available forced-command credential cannot grant the supported
  temporary recovery exception. No live lease was stolen, policy bypassed, task
  battery setting guessed, or external heartbeat sent. The next ordinary
  scheduled window is September 27. A new offsite copy still needs admission
  by that existing coordinator; local recovery readiness is independently proven.

### Installed repair verification

- Source commit `4c83e65c09fa4064e8569cdcc65a3b7a8b1c3bac`, isolated branch
  `fix/operational-review-20260921`; shared checkout changes preserved.
- Installed the reviewed 4.8.9 wheel in both the general Python environment and
  the scheduled graph/profile environment. Wheel SHA-256:
  `45595644d5aea76ecf1230ada82c86a829090e27a9ea2e67a613bcc9b95de6f7`.
  All 410 packaged Python files match in both environments. Prior wheel retained.
- Original live recall canary now **PASS**, target `mm-8aef` at rank 5, using the
  installed scheduled runtime and read-only SQLite. Query, target, provider and
  session diversity cap were not changed to obtain this result.
- Installed disposable lifecycle passed: capture, cited recall, valid observation
  support, retired-claim exclusion and stale-observation exclusion; temporary DB
  disposed. This is fixture lifecycle evidence, not production precision.
- Restarted only the verified MCP HTTP owner task. Health/readiness both 200;
  unauthenticated MCP 401; authenticated `tools/list` 200 with 51 tools. Existing
  long-lived stdio clients were not forcibly restarted; their loaded revision is
  unverified until their owners reconnect.
- Four scheduler task definitions plus operational-review configuration compare
  byte-for-byte unchanged by SHA-256. No provider, activation, budget or threshold
  changes. Wiki and JEV ingest shadow remain disabled.
- No live steward/Dreaming cycle was invoked for validation; quarantine behavior
  is source-tested and installed, with the next natural worker outcome unmeasured.

Evidence: `release-final/manifest.json`, `non-ml-final.log`,
`installed-general-identity.json`, `installed-scheduled-identity.json`,
`recall-repair-installed.json`, `installed-demo-after-repair.json`,
`mcp-restart.json`, `mcp-after-repair.json`, `config-after-repair-install.json`.
The post-install read-only operational probe completed at 18:14:39 UTC: **PASS**,
SQLite quick-check OK, zero FK errors, migration 25, graph/profile invariants and
original recall canary PASS, with zero database mutations. Its activation field
only describes the review process; scheduled worker flags were inspected separately
in the runtime evidence. Evidence: `live-review-after-repair.json`.
None of these repairs establishes observation precision or advances a
feature-success watermark.


### NAS recovery closed ? 2026-09-21, 19:20 UTC

This supersedes the earlier NAS admission blocker and partial backup verdict.
The documented administrative NAS credential was available and worked; the
restricted coordinator credential was not the only authorized access path.
Stopping at that restriction was an investigation error, not an operator action
that was required to complete this repair.

- Opened a two-hour recovery exception naming only MemoryMaster, retained the
  existing coordinator mutex and deadlines, and saved exact policy preimages.
  The normal NAS policy and Windows client command were restored byte-for-byte
  after completion; no active or blocked coordinator lease remains.
- The first ordinary-script attempt created a consistent staging artifact but
  inherited task priority 7 (low CPU/disk and memory priority). After bounded
  runtime priority corrections it still progressed too slowly at hashing and
  was deliberately stopped through its child process; the coordinator recorded
  exit 15 normally. Its staging file and failure evidence were preserved.
- Corrected the weekly task to priority 6. Exact exported XML comparison shows
  that only Priority changed. Added the existing backup-contract check for this
  setting; live configuration passes and a mocked priority-7 task is rejected.
  No trigger, credential, retention policy, deadline or catch-up setting changed.
- A second execution of the same weekly task finalized the already verified
  consistent snapshot completed today at 17:48:58 UTC. It started with normal
  CPU/disk priority without runtime intervention. This is explicitly a recovery
  upload of that snapshot, not a claim that new source data was captured at
  upload time. The task and NAS coordinator both finished with exit **0**.
- New NAS artifact `20260921/memorymaster-20260921T174557Z.db`, 7,585,837,056 bytes,
  SHA-256 `d7844db4b19e07e5bcd42c80e1a8e462e35ceab7d20a47c4a4b347c790d08d1c`.
  Fresh remote manifest/hash, SQLite quick-check and FK checks pass. Downloaded
  that actual NAS file into an isolated restore target: identical hash, matching
  table counts, zero FK errors, migration 25, and five cited confirmed recall
  results. Old backups and production memory data were preserved.
- The existing backup-health endpoint accepted its success receipt (HTTP 200,
  `ok=true`) only after restoration passed. This is backup health evidence, not
  a feature-quality watermark. The ordinary weekly schedule remains Sunday
  01:00. The expired September 14 one-time recovery task has no future trigger;
  its old exit code is historical evidence, not an outstanding current job.
- Infra source commit `615f1d0afafc518d0693493c1b7176ebac32b9b6` records preservation
  and the priority guard. The original September 20 scheduler-refusal cause is
  not retroactively attributed to priority; the current successful task run and
  the separately measured priority defect are the evidence for recovery.

Evidence: `nas-recovery-upload.json`, `nas-new-integrity.json`,
`nas-recovery-restored.json`, `nas-policy-restored.json`,
`nas-scheduler-recovered.json`, `backup-priority-repair.json`,
`nas-monitor-receipt.json`. **The NAS backup/restoration repair is closed.**
This does not change the independent semantic-quality findings of the review.
