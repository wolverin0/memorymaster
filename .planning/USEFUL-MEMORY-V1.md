<!-- doc-head: 4.8.8 deployed; activation verified and 4.8.9 installer closeout prepared -->
# Useful memory, reliable delivery
Covers: supported recall installation, real CI evaluation and source-level Dreaming sampling.
Key terms: Gemini-only, duplicate delivery, missed facts, label provenance, read-only.
Read when: accepting or deploying 4.8.8; ROADMAP.md remains the sole roadmap.
Status: activation verified; 4.8.9 packages installer repairs; profile catch-up continues.
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
