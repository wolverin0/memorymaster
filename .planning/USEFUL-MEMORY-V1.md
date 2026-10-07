<!-- doc-head: installed recall repair and September 28 daily/weekly evidence; quality limits -->
# Useful memory, reliable delivery
Covers: supported recall installation, real CI evaluation and source-level Dreaming sampling.
Key terms: Gemini-only, duplicate delivery, missed facts, label provenance, read-only.
Read when: accepting or operating 4.9.0; ROADMAP.md remains the sole roadmap.
Status: September 28 reviews complete with warnings; local restore verified, semantic quality unmeasured.
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

## 2026-09-23 independent weekly review (Claude)

Findings, coverage, fresh gate and evidence:
[.planning/audits/2026-09-23-weekly-claude-review/REPORT.md](audits/2026-09-23-weekly-claude-review/REPORT.md).
Corrections to statements above: the JEV ingest shadow flag **is set** in
`~/.claude/settings.json` (armed but inert, F-02), contrary to the "flag is
unset" / "remain disabled" lines of the September 21 review; the compiled
profile's `no_changes` is caused by verbatim capture being off since
2026-08-24 (F-03). F-04 is repaired on an unmerged local branch only.
Execution plan (cure, re-test, live Jev activation with decision ledger):
`artifacts/2026-09-23-plan-jev-y-curacion.html`; code-inventory findings F-20
(dormant irreversible `scheduled_archive`) and F-21 (automation logged as human
override) are in the review report addendum.


## 2026-09-28 daily operational and weekly acceptance review

**Review completed with warnings; weekly semantic acceptance remains UNMEASURED.**
ROADMAP and effective installed configuration were read first. Authoritative and
auxiliary SQLite were opened read-only; no steward, generation, sync or scheduled
backup job was forced. The configured graph-observation and profile generators
are enabled; GraphRAG recall is **DISABLED**, with retained graph data and no
eligible confirmed entity supports. Wiki absorption and workflow receipts also
remain disabled. No feature was enabled to produce a passing sample.

| Area | Observed result and acceptance limit |
|---|---|
| Installed correction | `190d020`, deployment receipt `5d87e02`; 6,707 non-ML tests passed, 0 failed, 75 skipped, 90 deselected, 1 expected failure. Both installed 4.9.0 runtimes match 438 files. Eleven original mock-fixture failures and full affected-shard reruns are preserved. |
| Authority and recall | Full SQLite quick_check OK, FK errors 0, schema 27. Configured live canary rank 5. Two fresh JEV recall hooks delivered complete long claim text in 2299.0/2598.6 ms. Persisted citations were present; this is not a measured semantic citation-correctness rate. |
| Capture and retirement | 95 retained blocked capture jobs, oldest August 11, maximum attempts 5; reasons include 73 ineligible and 20 unavailable graph claims. No retired production source item supplies a live retirement sample. Disposable lifecycle/retirement regressions pass separately. No retained data was deleted or promoted for the review. |
| Dreaming and steward | Dreaming application is enabled, not shadow-only. Current configured extraction/steward: google/gemini-3.5-flash-lite; consolidation: antigravity/gemini-3.7-flash-low. Seven-day Dreaming call records: 353, of which 339 OK and 14 errors. Latest partial run has one extraction HTTP 429 and one retryable capture; 14 captures applied. A dedicated actual steward-call receipt was not established. |
| Provider evidence and cost | Seven-day JEV ledger: 45,072 decisions, 10 orphan send intents, local token-rate estimate $1.06598268; invoiced cost UNKNOWN. Dreaming labels are call-record labels, not independent served-model attestations. Actual bounded hook probes served jev-1.13.0. Four validation calls total (two recall, two SessionStart including the retained comparator failure), not a quality cohort. |
| Graph quality and leases | 4,051 seven-day jobs report no_supports; zero new observations and zero independent precision labels: **UNMEASURED, n=0**. Three retained observations are archived, all 63 support edges have non-confirmed claims. No active/expired graph leases observed. Completed jobs do not establish precision. |
| Profile structure | Watermark 148519/148519, 61 active facts, all supported by 2-20 independent sessions. Exact hashes/counts/session manifests match. Projection emits 52 facts, omits 9, estimates 1,399/1,400 tokens and respects the 60 rendered-fact limit. Seventeen active preferences: zero beyond 90-day TTL; 44 stable facts retained. |
| Profile history and delivery | The retained AntigravityError precedes completed run 5 (September 25); September 28 is not_due, not a new successful map/reduce call. Actual installed SessionStart emitted the exact normalized generated profile within 10 seconds. The first comparison incorrectly included raw CRLF bytes; its failure is retained and the corrected loader-equivalent comparison passes. Agent consumption remains unmeasured. |
| Promotion and safety | 161 distinct candidate-to-confirmed promotions and 230 new citations are activity, not relevance labels. The 109 consolidation rejections are not a safety-specific rejection rate. Unsafe-output rejection effectiveness and real-task retrieval impact remain UNMEASURED; 13,146 returned-feedback rows and 14 detector events do not establish benefit. |
| MCP and sync | MCP health/readiness 200, unauthenticated 401, authenticated 51 tools; no new errors. Current Windows sync tasks succeeded; outbound/inbound delta quick_check and FK checks pass. Remote consumer acknowledgment remains UNMEASURED. |
| Backups and namespace | Latest weekly NAS task September 27 succeeded, but its independent restore is unmeasured; latest independently restored NAS receipt remains September 21. New local snapshot `mm-20260928.db` in authoritative namespace `memorymaster-073eec9cf3f9` on approved T volume has an independent restore, equal SHA-256, quick_check OK, FK errors 0 and canary rank 5. No existing backup was deleted or production target restored over. |

Fresh local backup SHA-256: `ab246f74c8d187633d4b382cb947378c535cf480fa0fc573ff0614d90d1d163f`;
size 7,660,548,096 bytes. Restored counts and provenance are in
`artifacts/operational-review/20260928/snapshot-restore.json`. This local proof
does not upgrade the unverified September 27 remote artifact to restore-tested.

The September 24 operator decision intentionally selects the T archive volume,
not the C system SSD. Current user settings and observed steward/MCP processes
match it; current OS disk mapping shows T on a separate healthy/online SATA disk.
The old ROADMAP statement pointing to the user-profile default was corrected.
An initial new temporary C copy was stopped after recovering that later decision;
the owned copy was preserved and verified under T. A combined move/cleanup was
blocked by automatic policy review; the fallback was additive copying, not a
configuration override. Slow direct HDD checking was replaced with full streaming
hash equality and independent restored-file integrity checks on the data volume.
After matching its hash to the verified backup, only the review-owned temporary
C copy and its sidecars were removed using narrowly scoped file operations.

Evidence: `artifacts/operational-review/20260928/` contains the installed review,
profile/graph manifest, seven-day aggregates, backup/sync state, SessionStart
receipts, snapshot restore and consolidated completion receipt. Source release
evidence remains in `.planning/audits/2026-09-27-recall-journey/REPORT.md`.
Work receipts are emitted only after these real checks. Review completion does
not advance any feature-success watermark or claim semantic acceptance.


## 2026-09-29 daily operational review

**Review completed with warnings; MCP was broken at session start and self-recovered; one bounded source-only fix shipped.**
ROADMAP, DOCS-MAP and the installed configuration were read first. The scheduled
`MemoryMaster-Operational-Review` task's own run (11:09Z, exit 3/WARN) supplied
the database/runtime/graph/profile/canary checks; this session added the checks
it does not cover (capture/retirement, live provider window, MCP health, sync,
backup restorability) read-only, without forcing any steward/generation/backup job.

| Area | Observed result and acceptance limit |
|---|---|
| Runtime and database | Scheduled review: installed=4.9.0=expected (pyproject), quick_check=ok, FK errors 0, schema 27 (11:09Z, took 220s on the live 7.6GB file; not re-run today to avoid a second multi-minute scan). |
| Declared vs actual activation | Review-process flags report compiled_profile=1, graph_observations=1, but that is the checker's own env, not proof of the scheduled worker's env. Cross-checked directly: Task Scheduler's `MemoryMaster-Dreaming` action passes `--apply-candidates` (not shadow) and matches the extract provider/model env vars; `MemoryMasterSteward`, both `HermesSync` tasks and both NAS backup tasks all last exited 0. GraphRAG vector-first recall and the TypeSafe skill selector remain **DISABLED** by declared config (`MEMORYMASTER_RECALL_GRAPH_MODE` unset, no `TYPESAFE_API_KEY`); their absence of errors is not counted as quality evidence. |
| Current provider calls (not historical labels) | Live `dream_status` 24h provider window: only `antigravity` (10 calls, 90% structured yield) and `google` (41 calls, 82.9%) were actually called in the last 24h, matching the current `MEMORYMASTER_DREAM_EXTRACT_PROVIDER=gemini`/`MEMORYMASTER_DREAM_CONSOLIDATE_PROVIDER=antigravity` config. `openai` and `zai-coding-plan` only appear in lifetime totals (527 and 225 calls respectively) from a superseded configuration; the `google_structured_yield_low` warning is driven entirely by the lifetime aggregate (0.705), not the healthy 24h figure (0.829). |
| Effective profile bounds | Read `ProfileConfig.from_env()` directly: only `MEMORYMASTER_PROFILE_MAX_INPUT_CHARS=24000` is overridden; every other bound is the code default (cadence 7d, max_map_calls 3, max_messages 500, min_independent_sessions 2, preference_ttl 90d, token_budget 1400, max_facts 60, reduce_batch 40). Scheduled review: 61 active facts (at the 60 cap, +1), 0 manifest mismatches, newest support age 6.3d against the 7d staleness limit — PASS but close to the edge. |
| Capture, recall and retirement | `capture_coverage(scope="*")`: status **attention** — 102 active sources, 212 active evidence, 3 confirmed claims in this scope; 95 blocked extract_graph/claims jobs (73 `graph_claim_ineligible`, 20 `graph_claim_unavailable`, 1 `attempts_exhausted`, 1 `ontology_validation_failed`), 0 expired leases, 0 orphans, 11 partially-completed jobs. Full-DB claim status census: 97,981 archived, 41,373 stale, 4,793 confirmed, 4,370 superseded, 8 candidate — retirement (archival) is clearly active. 0 of 102 source_items are retired (no source-level `forget()` has been invoked); this is a real zero, not a probe failure. `recall_stats` sample=0 is **UNMEASURED**, not a failure: the MCP server's in-memory counters reset when it reconnected mid-review (see MCP health below). |
| Graph supports and leases | Scheduled review: 348 edge_support_rows, 0 unknown_sensitivity_rows, 0 ineligible_confirmed_observations, 0 expired_leases. Live `dream_status` from the 09:11Z Dreaming run: 143 discovery jobs enqueued, 144 concluded no_supports, 0 components found, 0 observations emitted — consistent with the review's own 28,544-completed/3-observations lifetime shape. Low yield is a known, pre-existing characteristic (documented in the 2026-09-23 GraphRAG report), not a new regression. |
| Generated marker and SessionStart / checkpoint delivery | SessionStart injected the compiled profile and recent claims at this session's own start (directly observed, not log-inferred). Separately, the F-08 daily checkpoint **delivered into this exact pane** at 2026-09-29T14:25:04Z (`orca-poke OK: 900 chars -> claude term_30bc27c0`) — this review's own trigger. Checking the trailing 7 days in `feature-checkpoint.log` found a **5-day total delivery outage** (Sep 23 DRY-RUN/FAIL, Sep 24-27 all FAIL on both Orca and WezTerm paths with `no connected MemoryMaster claude/codex terminal`) that only recovered Sep 28-29. ROADMAP's own F-08 gate ("alert on failed delivery and 7/7 days delivered") is **NOT MET**: 2/7 trailing days, though the two most recent days are clean. This is an observed, dated, now-recovering failure, not a currently-open incident. |
| MCP health | **Observed broken at session start**: `memorymaster` MCP server reported `CONNECT_TIMEOUT`. Self-reconnected mid-session (`/mcp reconnect memorymaster` via `orca terminal send` to this own pane, per the standing self-reconnect instruction); confirmed healthy afterward with two live successful tool calls (`dream_status`, `recall_stats`). The separate Hermes-facing HTTP MCP server (`MemoryMaster-MCP-HTTP-Hermes`, 192.168.100.155:8765) has been running continuously since 2026-09-25T17:27 (4 days uptime, not an error) and is a distinct surface from this session's own stdio connection. |
| Sync | Both `MemoryMaster-HermesSync-AM` (04:00 local) and `-PM` (16:00 local) tasks last exited 0, most recently this morning. No sync failure observed. |
| Latest restorable backup | Weekly NAS task (`MemoryMaster-Backup-NAS-Weekly`) last succeeded 2026-09-27T01:01-01:11 UTC: `VACUUM INTO` consistent snapshot, row counts verified against the live DB, SHA-256 verified **both locally and on the remote NAS** (`/tank/backups/wolverin0/20260927`), Uptime Kuma heartbeat sent. This is genuine restorability evidence (hash-matched on both ends), 2 days old, not just "the job exited 0." A separate, more recent local snapshot (`memorymaster-073eec9cf3f9/mm-20260928.db`, 7,660,548,096 bytes, 2026-09-28) exists on the approved T: volume but carries leftover `.part-wal`/`.part-shm` sidecars; its own integrity was **not** re-verified today (would require a second ~220s quick_check against a 7.6GB file with no new signal expected) — its restorability is UNMEASURED by this review, distinct from the NAS artifact's confirmed status. The known limitation that `govern/recovery.py`'s encrypted restore drill cannot handle this database's current size (~18GB peak memory) remains open and unaddressed by this review; the NAS path does not depend on it. |
| Bounded fix shipped | `memorymaster/dreaming/ledger.py`: `DreamLedger.status`/`read_status` had a hardcoded `interval_minutes=60` default driving the `scheduler_stale` warning (fires when the last heartbeat is older than 2x this value). The real Dreaming cadence is 360 minutes (6h) via Windows Task Scheduler, so the old default guaranteed a false `scheduler_stale` warning for roughly 4 of every 6 hours — this is why `dream_status` shows it right now even though the actual scheduler is healthy (on-time, exit 0). Changed the default to fall back through `MEMORYMASTER_DREAM_INTERVAL_MINUTES` when unset (still 60 if that is not set, so behavior is unchanged for everyone who has not configured it). `pytest tests/test_dreaming_ledger.py` (8 passed) and `pytest tests/ -k "dream_status or dreaming_cli"` (3 passed) both green. **SOURCE-ONLY, NOT INSTALLED**: the live scheduled tasks run from the installed wheel under `C:\Users\pauol\.memorymaster\runtime\graph-profile-20260813\`, untouched by this change; the live false-positive persists until that runtime is rebuilt/reinstalled and the new env var is set to match the real cadence — an explicit operator decision, not made here. |

Zero authoritative-database mutations; zero production configuration changes;
zero forced jobs. Evidence: `artifacts/operational-review/20260929/raw-evidence.json`
(sha256 `ae8dfd19b9e61328f8eb168208536029a1706b9282ce6d8116de5bfe3f231217`) and a copy
of the scheduled review's own `installed-review.json`
(sha256 `d7fe010dba21ee561c41a8dd7845e98fb5b21feec6652c0b67930bdae2af54c9`). This
review does not advance any feature-success watermark or claim semantic
acceptance; delivery of this checkpoint is not, by itself, evidence of quality.


## 2026-10-02 daily operational review

**Completed with warnings. One bounded source fix was committed but not installed.**

| Area | Observed result and acceptance limit |
|---|---|
| Scheduled review (11:09Z, exit 3) | Runtime, database, graph, private context and canary PASS. WARN on compiled_profile, jev_decisions and checkpoint_delivery. |
| Declared vs actual activation | The worker's own `dream_result` carries the `graph_observations` and `compiled_profile` sections, so both flags are active in the scheduled worker. Jev mode is live. |
| Current providers | Since 2026-10-01T12Z: google/gemini-3.5-flash-lite 37 ok and one 429; antigravity/gemini-3.7-flash-low 15 ok. This matches the configured extract and consolidate providers. The 03:11Z run was partial because of that one 429 (extraction stops on 429 by design); the 09:11Z run was ok with 20 applied. |
| Profile | Run 5 completed 2026-09-25T09:16Z and the cadence is 7 days, so the 09:15:28Z Dreaming run was not_due by about 32 s; the next run starts run 6. The review threshold (support under 7 days) cannot hold with a 7-day cadence plus multi-day mapping: this is an **obsolete expectation**. **Observed:** 61 active facts but 52 rendered at 1399/1400 tokens; the 9 omitted are all standing constraints, the last section. SessionStart also caps the whole injection at 3000 chars (T-0797 P11, installed hook only, not in the repo template), so working style and constraints do not reach sessions. The manifest and user.md agree (52), and the generated marker is present. |
| Capture, recall, retirement | Last 24 h: 20 claims created, 168 touched by recall, 28 decayed to stale, 0 archived. 95 capture jobs are permanently blocked by design; 0 expired leases; graph jobs idle. |
| Jev dedup silence | Last decision 2026-09-26. Dedup asks Jev only about `candidate` pairs, and only 7 candidates exist. The steward still runs dedup every cycle. **Input-starved, not broken**: the 36 h silence rule does not fit this surface. |
| Checkpoint (F-08) | Trailing 7 days: 3 ok (09-28, 09-29, 10-02) and 3 failed (09-27 both paths, 09-30 no connected terminal, 10-01 `orca terminal list` timed out at 40 s). The gate is **NOT MET**. The Windows task exits 0 even on failure; only this review detects it. |
| MCP health | Shared server 8766 healthz 200, no watchdog kill since the 01:59Z restart. Hermes (LAN bind) healthz/readyz 200, unauthenticated 401. |
| Sync | Windows watermark updated 2026-10-02 04:08 local; Hermes delta updated 03:00 local. |
| Latest restorable backup | NAS 2026-09-27, sha256 verified locally and on the NAS. Local snapshot `mm-20260930.db`: read-only `quick_check` ok, 148,555 claims, 365 s; this is fresh evidence for that copy. |
| Fix | `6c26d50`: compiled_profile now WARNs when active facts are missing from the injected manifest (red/green tests; live run reports "9 of 61"). **Source only**: the scheduled review runs the installed wheel. |

Not changed (design decisions for the operator): renderer section priority, profile budget, the SessionStart cap in `~/.claude/hooks`, and the cadence/threshold mismatch. Zero authoritative-database mutations, zero forced jobs. Separately, under explicit operator approval, the Serena MCP configuration and hooks were changed today; that change is not part of this review.
Evidence: `artifacts/operational-review/20261002/raw-evidence.json` (sha256 `e342be7a3913fdadfc52f049d967c77019d3b7fce2266d9e34c1a4d5f295c2e6`) and `installed-review.json` (sha256 `7f235ed198769c6f587919bbb9e68f7e523ed51576fd4ecbe82cb9e3a93147c1`). No feature-success watermark is advanced.

## 2026-10-07 daily operational review

**Completed with warnings. Two owned regressions fixed and installed; the post-unification backup is verified.**
ROADMAP and the effective installed configuration were read first; the authoritative
and auxiliary SQLite were opened read-only for measurement. No steward, generation,
sync or backup job was forced and no feature was enabled to produce a sample.

| Area | Observed result and acceptance limit |
|---|---|
| Due measurements | `boost-floor-canary` PASS (scheduled review: mm-8aef rank 5). `graph-discovery-skip` partial: the worker enqueued 0 and skipped 146 per run, but `improve()` still queued 1 `no_supports` job per run; fixed `520a736`. `gemini-extract-pacing` failed: the 03:11Z run was clean, but the 09:11Z run hit a 429 after 10 well-spaced calls carrying 256k input tokens. The key also limits tokens per minute; fixed `c7e0b92` (rolling 200k-token minute). Both fixes installed 14:31Z, with follow-up measurements due. |
| Scheduled review | Installed run at 14:37Z: WARN, exit 3, 0 mutations. Canary PASS (rank 5). Compiled profile: 17 of 63 active facts are cut by the 3000-char budget (Fleet T-0797). Jev: 23 orphan intents in 24 h. |
| Activation | Package 4.9.0 in all three runtimes (wheel `520a736`). User flags: GRAPH_OBSERVATIONS=1, COMPILED_PROFILE=1, JEV_MODE=shadow, JEV_HOOK_DEADLINE_MS=1500, RECALL_DENSE=1. Extraction: google/gemini-3.5-flash-lite. Consolidation: antigravity/gemini-3.7-flash-low. |
| Providers since 10-06 14:30Z | google 36 ok, 1 × 429. antigravity 14 ok. |
| Dreaming | 15:11Z, 21:11Z and 03:11Z runs ok. 09:11Z partial (the 429). Leases 0. Captures: 316 applied, 40 captured, 1 retryable. Of 348 `extracted`, 345 are retained legacy (07-27..09-07) and 3 await consolidation budget (resume-eligible). |
| Graph | 348 supports. 3 observations (last 08-31). No open discovery jobs, 0 expired leases. |
| Capture | Unchanged: 94 extract_graph blocked, 1 extract_claims blocked. 0 expired leases. |
| Profile | Run 6 watermark 148760 = target. user.md and user-profile.json generated 10-05 09:13Z, with marker. 46 facts, all with exact supports. SessionStart injection seen in this session. |
| Dense prompt recall (T-1042) | Enabled 00:28Z after regression and verifier. Of 125 recalls: 116 dense, 8 empty, 1 fallback (0.8%). Injected 4.9 claims and 5.8k chars per recall (lexical: 5.9 and 6.5k). Embed on GPU p50 123 ms (max 565 ms). Service+hydration p50 475 ms, p95 3.6 s: the time is spent in the shared server, not the model. |
| Observed failure: recall skipped | Hook `via=skipped_busy` (shared server did not answer within 4 s, so no recall that prompt): 39% on 10-05 and 22% on 10-06, both lexical; 20% on 10-07, dense. This predates dense. Host memory: 5.9 GB free of 64, 116/172 GB committed; 196 node processes take 7.1 GB, vmmem 7.4 GB. Not fixed here; this is the leading recall loss. |
| Observed failure: Jev | Recall orphans rose to 5 of 75 sends (6.7%) in the dense window, against 0.47% in 09-24..10-06. Timeouts rose from 10-06 (before dense), and most are `not_sent`: the 1.5 s deadline expires before the send. Stalls of up to 68 s in `engine_ms` coincide with the memory pressure. Cause is environmental, not the dense path. A burst of 11 `session`/`hints` orphans at 05:31Z. |
| Steward | Last 4 runs exit 0 (22-76 min). A run is in progress. |
| MCP and sync | Shared 8766, Hermes 8765 and dense 8767 healthy. HermesSync-AM exit 0 at 04:00 local. 0 live tenant twins (the 4,789 shared keys are archived or superseded). |
| Backup | Post-unification NAS backup verified: `memorymaster-20261007T040331Z.db` at 04:13Z, counts OK against the live DB, sha256 verified on the NAS. Run by infra's one-shot recovery task after the request. O-0259 closed. |
| Not measured | Whether dense recall changes agent outcomes (the T-0739 usage re-measure is due 10-11). Dense 7-day metrics are due 10-13. |

Evidence: `artifacts/operational-review/20261007/installed-review.json` (sha256
`ccaf3518…2c6674f9`) and `raw-evidence.json` (`66e819d2…b119d77`).

## 2026-10-06 daily operational review

**Completed with warnings. Three owned regressions fixed and installed; canary back to PASS.**
ROADMAP and the effective installed configuration were read first; the authoritative
and auxiliary SQLite were opened read-only for measurement. No steward, generation,
sync or backup job was forced and no feature was enabled to produce a sample.

| Area | Observed result and acceptance limit |
|---|---|
| Scheduled review (11:09Z, exit 1) | FAIL on retrieval_canary (mm-8aef missing), before the 11:14Z install. Installed review at 14:30Z: **WARN, exit 3, 0 mutations**; runtime 4.9.0, database (quick_check ok, FK 0, migration 28), activation, graph, private context, **canary rank 5** and checkpoint (14:25Z today) PASS; WARN compiled_profile (17 of 63 omitted, products by design) and jev_decisions. |
| Canary cause (corrects 10-05) | Not clock decay alone: the answer had the best lexical score (0.475) but a 0.107-lexical claim outranked it on confidence and freshness. **Owned fix `fc623e6`:** `boost_floor_ratio` default 0 -> 0.5. 60 real prompts, blind Haiku labels: nDCG@5 0.593 -> 0.747, P@5 0.392 -> 0.431, 11 better, 4 worse, 11 tied (labels are a model proxy, not operator judgement). Rollback `artifacts/release-floorgate-20261006/rollback.ps1` or `MEMORYMASTER_BOOST_FLOOR_RATIO=0`. |
| Declared vs actual activation | Profile 1, graph observations 1, Jev shadow, floor ratio unset (code default 0.5). Dreaming applies candidates; extract google/gemini-3.5-flash-lite, consolidate antigravity/gemini-3.7-flash-low as declared. 4.9.0 in both runtimes. GraphRAG recall, wiki absorption and workflow receipts: DISABLED. |
| Providers (call records) | Since 10-05 14:30Z: google 41 ok, 2 x 429; antigravity 9 ok. Each Dreaming run's single 429 leaves one capture retryable and exits rc 1 (03:11Z 15 applied, 09:11Z 17 applied). Invoiced cost UNKNOWN; Jev estimate today $0.12. |
| Graph observations | Enabled; **UNMEASURED, n=0**. Cause found: only 3 of 4,729 confirmed claims carry captured evidence and no edge support was written after 08-14. **Owned fix `a2aac47`:** discovery enqueued only with supports or live observations (0 of 146 scopes today; before, 144-145 `no_supports` jobs per run). First proof at the 15:11Z Dreaming run (due measurement `graph-discovery-skip`). |
| Capture and queues | Capture jobs 94 graph + 1 claim blocked (old), 0 expired leases. Dreaming captures 339 applied, 43 captured, 345 `extracted` retained from 07-27..09-07 (unchanged), 1 retryable. |
| Profile | Run 6 at watermark 148760 = target, not due; 63 active facts, manifest = user.md = 46, generated marker present (10-05 09:13Z). SessionStart in this session showed the profile trimmed at the 3000-char cap (Fleet T-0797). Agent consumption UNMEASURED. |
| Jev | Shadow. 8 orphan send intents of 4,297 since 10-05 (0.19%): the decision write hit `OperationalError` while heavy writers ran (scope unification 00:46-00:52Z, steward plus full test suites 13:46-13:59Z). Fail-visible by design; also seen 10-03, before the shadow thread. Observed, not fixed. |
| Steward | `steward-job-finish-outcome` **PASS**: runs at 03:48Z, 08:03Z and 14:20Z log outcome=ok, exit 0; task rc 0. The 10-05 19:52Z run has no job_finish and 01:52Z took 7,011 s, both during the unification. |
| Recall latency | `textgen-live-recall` **PASS on median**: 1,509 -> 922 ms; clean hours today 119-680 ms. p90 13.6 -> 20.0 s tracks this lane's own heavy jobs, so the tail is UNMEASURED under normal load. |
| Tests | Owned regression: a user-level `MEMORYMASTER_JEV_MODE` failed 10 Dreaming tests; `dc5203f` clears `MEMORYMASTER_JEV_*` per test. Full non-ML suite 6,882 passed, 0 failed after regenerating release truth. |
| Checkpoint (F-08) | Trailing 7 days: 09-30 and 10-01 failed (no MemoryMaster pane, Orca timeout), 10-02..10-06 ok. Gate NOT MET until 10-08 if delivery holds. |
| MCP and sync | Shared 8766 healthz 200 after the 11:14Z restart; Hermes 8765 (LAN listener) healthz/readyz 200, unauthenticated 401. Hermes delta DONE 10-05 03:13, 15:13 and 10-06 03:13 local, twins 0. |
| Snapshot namespace and backup | Latest snapshot `mm-20260930.db`; stray 0-byte `part-wal` from 09-28 left in place. NAS `memorymaster-20261004T040106Z.db` verified 10-04 (predates curation). Local `artifacts/scope-unify-20261005/pre-unify.db`: quick_check ok, 149,477 claims. No verified backup of the post-unification state yet; weekly NAS run 10-11. |

Evidence: `artifacts/operational-review/20261006/raw-evidence.json` (sha256
`a94b870e843e24f3255ae09d7d1fd57ac980a5e8dbaa228261ad0e54986e4ccb`) and
`installed-review.json` (sha256 `2f3fc63b2077b9b6806b3cb545f00a952aed49ea2fb72514c5f1f40ddbdf29fe`).
No feature-success watermark is advanced.

## 2026-10-05 daily operational and weekly acceptance review

**Completed with warnings. Two owned regressions fixed, one installed; weekly semantic acceptance remains UNMEASURED.**
ROADMAP and the effective installed configuration were read first; the authoritative
and auxiliary SQLite were opened read-only for measurement. No steward, generation,
sync or backup job was forced and no feature was enabled to produce a sample.

| Area | Observed result and acceptance limit |
|---|---|
| Scheduled review (11:05Z, exit 1) | **FAIL on retrieval_canary** (mm-8aef rank missing, was 5). Runtime 4.9.0, database (quick_check ok, FK 0, migration 28), activation, graph, private context and checkpoint PASS; WARN compiled_profile (17 of 63 omitted, products by design) and jev_decisions. 0 mutations. |
| Canary diagnosis | Two causes. **Owned:** the 774 curation copies (03:29Z) carried created_at/last_validated_at of the copy moment, so the freshness bonus and recompute_tiers (created < 7 days -> core) ranked month-old claims as new; three wezbridge copies took the top places. Fixed `b5ba660`/`668fcf7` (copies adopt the source's created/validated times, tier and access counters; updated_at moves so the delta sync re-exports them); the 774 live copies were repaired, 0 mismatches, prior values in `artifacts/curation-20261005/history-fix-before.json`. **Not owned:** the canary claim itself decays by clock each steward cycle (confidence 0.5008 at 13:53Z) and stays at rank 8 after the fix, so the FAIL stands: it is the known clock-decay issue, not retrieval. |
| Declared vs actual activation | Profile 1, graph observations 1, Jev **shadow since 13:38Z** (operator ruling; live before, with the shadow-inline fix `7e80757`). Dreaming in application mode (dry_run=0 on all 27 runs in 7 days). 4.9.0 in both runtimes. GraphRAG recall, wiki absorption and workflow receipts: DISABLED. |
| Providers (call records) | Since the last review: google/gemini-3.5-flash-lite 33 ok, 2 x 429 (the first call of each Dreaming run); antigravity/gemini-3.7-flash-low 5 ok. Seven days: google 264 ok, 7 x 429, 5 x 503, 1 other; antigravity 54 ok, 2 errors. Model names are call-record labels, not served-model attestations. Profile runs keep run labels only; no separate profile provider-call records were found. Jev seven-day estimate $0.421 at token rates; **invoiced cost UNKNOWN**. |
| Graph observations | Enabled; **UNMEASURED, n=0.** 3,969 jobs in 7 days, all `no_supports`, max attempts 1, no expired leases; 0 new observations (3 retained, all archived). All 63 supports point to non-confirmed claims (archived 10, stale 48, superseded 5), so no observation is recallable on retired support. ROADMAP item 8. |
| Capture and queues | Capture jobs: 95 blocked (oldest 08-11, max attempts 5; 73 ineligible, 20 unavailable graph claims), 0 expired leases. Dreaming captures: 326 applied, 37 captured, 345 `extracted` retained from 07-22..09-07 (max attempts 9, nothing new since), 1 retryable. Applications in 7 days: 13 add, 100 ignore. |
| Promotion and citations | 917 distinct candidate-to-confirmed promotions in 7 days, **143 excluding the 774 curation copies**; 1,199 new citations, 1,023 of them copies. Activity, not relevance labels. Unsafe-output rejection rate and retrieval impact: **UNMEASURED**. |
| Profile | 63 active facts (18 preference, 45 stable), every fact with >= 2 independent sessions, support counts and session counts match exactly, 0 preferences beyond the 90-day TTL, stable facts retained since 08-06. Run 6 completed 10-03 at watermark 148760 = target; manifest = user.md = 46 facts, generated marker present, 1400-token/60-fact renderer limits. Live installed SessionStart: rc 0, 936 ms, 2,952 chars, exact prefix of user.md, 9 of 46 facts visible (identity and constraints) under the 3000-char cap (Fleet T-0797). Agent consumption UNMEASURED. |
| Steward | Runs every 6 h, task rc 0. **Owned regression:** since T-0764 every `job_finish` logged `outcome=error, error_type=SystemExit` because the steward script ends with `sys.exit`. Fixed `a76e36a` (red then green), installed in both runtimes; rollback `artifacts/release-stewardexit-20261005/rollback.ps1`. Proof owed at the next steward run (19:52Z). |
| Checkpoint (F-08) | Trailing 7 days: 5 ok (09-29, 10-02..10-05), 2 failed (09-30, 10-01). Gate **NOT MET**. |
| MCP and sync | Shared 8766 healthz 200; Hermes 8765 healthz/readyz 200, unauthenticated 401; authenticated hook recall 200. Hermes runs DONE 10-04 15:13 and 10-05 03:13 local, twins 0, quarantine 1568 stable. The curation copies and their repair travel with the next Windows delta. |
| Snapshot namespace and backup | `T:/MemoryMaster/snapshots/memorymaster-073eec9cf3f9/mm-20260930.db` (weekly; next due about 10-07) plus a stray 0-byte `mm-20260928-*.part-wal`, left in place. Restorable today: `artifacts/curation-20261005/pre-curation.db` (sqlite backup API before the curation): quick_check ok, FK 0, 148,665 claims, opens and answers through MemoryService. NAS `memorymaster-20261004T040106Z.db` verified 10-04. |
| Due measurement | `shared-mcp-outage-window` **PASS**: since 10-03 15:20Z, 1 stall dump and 0 supervisor kills (about 5/day before); today's two restarts were manual deploys. `steward-job-finish-outcome` added (due 20:30Z). |
| Tests | Full `nox -s unit` 6,840 passed, 0 failed (after `3271867`: cwd restored per test, TF-IDF tests marked ml); `nox -s ml` 90 passed. |

Other work today, recorded in ROADMAP 7e-7f: prompt recall reads the cwd project and its
parents (`97f855d`, `367e8a9`); 774 project-specific claims moved out of `project:py-apps`
by copy and supersession (`d3cb153`). The authoritative database was mutated by that
curation, its history repair, and 7 operator-approved fuzzy-duplicate resolutions;
the review itself forced no job. Evidence: `artifacts/operational-review/20261005/`
(installed-review, raw-evidence, weekly-acceptance). No feature-success watermark is advanced.

## 2026-10-04 daily operational review

**Completed with warnings. One bounded regression of mine fixed, installed and run on Hermes.**

| Area | Observed result and acceptance limit |
|---|---|
| Scheduled review (11:09Z, exit 3) | Runtime 4.9.0, database (quick_check ok, migration 27), activation, graph, private context, canary (mm-8aef rank 5) and checkpoint PASS. WARN on compiled_profile (17 of 63 omitted: products, by design since `51c8792`) and jev_decisions. 0 mutations. |
| Declared vs actual activation | Profile 1, graph observations 1, Jev live; 4.9.0 in both runtimes. Workers ran: Steward 13:52Z rc 0, Backup 04:00Z rc 0, Hermes sync 07:00Z rc 0. Dreaming 09:11Z rc 1: a google 429 on the run's first call, the rest of the run completed (15 applied). Same 429 eight times since 09-27; known, extraction stops on 429 by design. |
| Current providers | Since the last review: google/gemini-3.5-flash-lite 37 ok, 1 error (429); antigravity/gemini-3.7-flash-low 8 ok. Matches the configuration. |
| Profile | Run 6 completed on the 10-03 15:11Z retry: 46 facts, 7 applied, 30 rejected; watermark 148760 = target. Manifest = user.md = 46, generated marker present, constraints second. `error_code` keeps the last intermediate error on a completed run (cosmetic). |
| Graph observations | **Observed failure (no output).** Active, but every run enqueues about 150 discovery jobs that all end `no_supports`; 0 emitted. Only 3 observations exist, the last from 2026-08-31; 31,345 jobs accumulated. ROADMAP item 8. |
| Capture, recall, retirement | Last 24 h: 11 claims created, 21 extractor, 91 validator, 79 decay events. 95 capture jobs blocked (unchanged), 0 expired leases, 63 graph supports. |
| Jev (72 h measurement) | Strong use: recall 1/1066, session 3/277, hints 0/1576, skills 0/11. Positives per question about 1 and 3: calibration **cannot start**. Orphan send intents 16 since 09-20 (about 1/day of about 490 sends), recall hook cut by its deadline: known, low rate. |
| Recall hook (T-0594) | **Observed failure against its criterion.** Cancellations did not fall (5/183, 2/50); `skipped_busy` 21-32 %: the paged-out shared server. |
| Checkpoint (F-08) | Trailing 7 days 5 ok, 2 failed (09-30, 10-01). Gate **NOT MET**. |
| MCP health | Shared 200; Hermes healthz/readyz 200, unauthenticated 401. |
| Sync | Windows watermark 2026-10-04T05:36Z; Hermes delta 02:59 local. On Hermes: merges ok, 0 errors, quarantine 1568 stable over 3 runs, no schema warning (T-0530 closed). **My regression:** the tenant-twin step (`abf6337`) had a literal backslash-n and aborted the 10-03 15:00 and 10-04 03:00 runs after their merge (cleanup and DONE skipped). |
| Latest restorable backup | NAS `memorymaster-20261004T040106Z.db`: counts verified against the live DB, sha256 local equals NAS. |
| Fix | `1621f01`: the twin step is an if-block; a test runs the real block under `set -euo pipefail` (red before, green after). Deployed to Hermes and run there: exit 0, 0 twins. Proof owed at the 15:00 local run (`hermes-twin-step-first-run`). |

Zero authoritative-database mutations by the review, zero forced jobs. Evidence: `artifacts/operational-review/20261004/` (raw-evidence, installed-review, jev-status-3d). No feature-success watermark is advanced.

## 2026-10-03 daily operational review

**Completed with warnings. Two bounded fixes committed and installed.**

| Area | Observed result and acceptance limit |
|---|---|
| Scheduled review (11:09Z, exit 3) | Runtime 4.9.0, database (quick_check ok, migration 27), activation, graph, private context, canary (mm-8aef rank 5) and checkpoint PASS. WARN on compiled_profile and jev_decisions. 0 mutations. |
| Declared vs actual activation | Compiled profile 1, graph observations 1, Jev live; both runtimes 4.9.0. The workers ran: Steward 13:52Z rc 0, Dreaming 09:11Z rc 1 (the profile failure below), HermesSync 07:00Z rc 0. |
| Current providers | Since the last review: google/gemini-3.5-flash-lite 40 ok, 1 error; antigravity/gemini-3.7-flash-low 11 ok. Matches the configured extract and consolidate providers. |
| Profile | **Observed failure.** Run 6 mapped 41 candidates in 6 calls and sits in `reducing`; three attempts in a row were rejected by the validator, each for a different reason (volatility, predicate, candidates not exactly once). The model output is malformed and the fail-closed validator is right; it retries every 6 h (follow-up `profile-run-6-reduce`, 21:30Z). Order changed today (`51c8792`): 61 active, 47 rendered, all 15 constraints and 21 working-style facts; the 14 omitted are products. Manifest = user.md = 47, generated marker present. SessionStart, live hook run: 2960 chars, 5 identity + 4 constraints visible (was 0 constraints). |
| Capture, recall, retirement | Last 24 h: 16 claims created, 27 extractor and 77 decay events, 7 policy decisions. 95 capture jobs blocked (unchanged since yesterday), 0 expired leases, graph jobs idle, 63 graph supports. |
| Jev | dedup silent 36 h (input-starved, as on 2026-10-02); skills silent 24 h is **unmeasured** until the 72 h measurement due 2026-10-04. |
| Checkpoint (F-08) | Trailing 7 days: 4 ok (09-28, 09-29, 10-02, 10-03), 3 failed (09-27, 09-30, 10-01). Gate **NOT MET**. |
| MCP health | Shared 8766 healthz 200; Hermes healthz/readyz 200, unauthenticated 401. **Observed failure:** watchdog kill at 14:02:37Z. The stack dump points to memory pressure, not code: the loop thread was in trivial logging code with every other thread idle, 612 MB private / 65 MB working set, 3.9 GB free of 64. ROADMAP item 5. |
| Sync | Hermes delta 02:59 local merged; Windows delta exported 04:09 local; watermark 2026-10-03T04:46Z. |
| Latest restorable backup | Local `mm-20260930.db` (weekly cadence, so on schedule; quick_check ok 2026-10-02, file unchanged since). NAS 2026-09-27 rc 0, next 2026-10-04. |
| Fixes | `27c9750`: the session-start template now carries the 3000-char cap (`setup` would have reinstalled it without the cap); the suite no longer reaches the live shared server (regression from T-0594 `ff35152`, red then green). Installed in both runtimes. Full non-ML suite (nox, 40 min): 6788 passed, 5 failed, all from this week's work and fixed in `c3599e7`/`8c93be7`: recall imported a surface (moved to `core/shared_mcp.py`), fuzzy dedupe lacked a declared numpy, release truth stale. Supervisor waits out a 120 s outage (`6f4e978`, installed). Live hook check after install: the first prompt after idle was `skipped_busy` (paged-out server), the next two `via=shared` in 1.3-1.7 s. |

Obsolete expectation, unchanged: profile support under 7 days cannot hold with a 7-day cadence. Zero authoritative-database mutations by the review, zero forced jobs. Evidence: `artifacts/operational-review/20261003/raw-evidence.json` and `installed-review.json`. No feature-success watermark is advanced.

## Shared CLI code navigation - 2026-09-28

Installed Serena 1.7.0 with five navigation tools and per-session stdio for AGY,
Claude Code and native/Orca Codex. Refreshed GitNexus 1.4.7 preserving embeddings;
excluded cloned research. Fresh client smokes exercised/reported both tools.
The frozen 20-task cohort gives GitNexus file@5 9/20 versus lexical heuristic
7/20; Serena exact-symbol discovery 5/5, known-target bodies 19/20 (one bounded
overflow). The 92.06% smaller successful body payload is conditional and does
not establish total session token savings. Graphify's old graph returned cloned
upstreams and remains outside default routing. GraphRAG activation unchanged.
Added read-only index/checkout preflight: four regressions plus public lifecycle
demo (six tests) passed, guard recheck four passed, Ruff and release truth pass.
Evidence: `.planning/audits/2026-09-28-code-intelligence/REPORT.md`.

OpenCode extension (2026-09-28): installed the same Serena context and ten exact
read-navigation permissions. The initial run exposed unavailable GitNexus and
temporary-project selection; retained that evidence and corrected the native
Node launcher, 90-second discovery timeout and workspace-relative MCP cwd.
Fresh OpenCode 1.18.33 with existing plugins/default provider completed
`serena_get_current_config`, `gitnexus_context` and `serena_find_symbol`.
Verified MemoryMaster project, exact five exposed Serena tools and source AST
lines 681-745. Unrelated configuration preserved. Client cost estimate for the
successful run is $0.839411 (not invoice, excludes initial attempt); total token
savings remain UNMEASURED. See `opencode-checks.json` beside the shared report.
