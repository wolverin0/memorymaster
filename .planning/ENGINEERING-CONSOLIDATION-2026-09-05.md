<!-- doc-head: 4.8.7 deployed; 88-action review, restored backup and remaining fleet/evaluation limits -->
# Engineering consolidation: 2026-09-05
Covers: verified local 4.8.7 deployment, bounded Dreaming review, backups, installed-hook corrections and remaining limits.
Key terms: source versus runtime, public retirement, launcher activation, historical evaluation, Gemini-only consolidation.
Read when: reviewing this change or choosing the next bounded improvement.
Authority: implementation evidence only; ROADMAP.md remains the sole roadmap.
<!-- /doc-head -->

## Scope and outcomes

The starting checkout was main at d98da8e, zero commits behind the locally
recorded origin/main. Changes are isolated in chore/consolidate-memory-workflow-20260905;
the unrelated delta-exchange directory is untouched.

| Action | Evidence / outcome |
|---|---|
| Remove duplicated instructions | Short AGENTS with actual package paths; detailed verification and shared GitNexus guidance in docs/development.md; CLAUDE links rather than repeats |
| Correct status | PPR-7/profile shipped in 4.7; workflow code integrated on main; installed 4.8.5 does not imply enabled generation |
| Fix profile limits | ProfileConfig.from_env used 800/40 while direct construction used 1400/60; both now share projection defaults and preserve explicit overrides |
| Strengthen existing demo | Public remember -> linked fixture extraction -> promotion -> cited recall -> public forget -> recall exclusion; no new runner |
| Update scheduled review bodies | Versioned scripts/checkpoints templates copied exactly to existing daily/weekly message files; models/bounds read from effective configuration |
| Repair current target discovery | Current pane independently verified; restored unique memorymaster tab title. No task fired and no delivery-success claim made |

No database schema, existing live claims, worker application flag, provider selection,
generation flag, CI gate, or dependency was changed. No new scheduler or service
was introduced. Source fixes require normal integration/package deployment
before affecting installed processes. The configuration root cause was recorded
through governed ingest as new candidate mm-4064.

## Verification

- Baseline public-demo/provider-batch tests: 5 passed.
- New regression tests before fixes: 2 failed / 2 passed. Failures identified
  the default mismatch and missing post-retirement recall evidence.
- Focused demo/profile/observation/public-facade suite after fixes: 52 passed.
- Fresh source CLI demo: exit 0; three captures processed, cited confirmed
  observation, ordinary observation exclusion, retired-claim exclusion and
  stale-observation exclusion all observed.
- Ruff on changed Python files passed. Release truth regenerated using the
  existing generator, preserving CI's protection.
- Additional intake/MCP/provider-batch/reduce-resume/release-metadata checks:
  32 passed. Combined post-fix focused coverage: 84 passed.
- Release truth freshness and git diff whitespace checks pass.
- Installed daily and weekly prompt files exactly match the versioned templates.
- GitNexus change detection reports low risk; its symbol matcher also lists
  unchanged same-named README files, so the staged Git file list is the exact scope.

This is fixture evidence for the public lifecycle, not provider answer-quality
or live client delivery evidence. The graph fixture deliberately supplies
structured relationships and deterministic promotion.

## Live evidence inspected read-only

Operational artifact: 2026-09-05T09:55:40Z, installed=expected=4.8.5,
quick_check=ok, FK errors=0, migration=24, 41 recent claims scanned / 0 private
context matches, retrieval canary rank=4. Its profile check found 52 active
facts, 565 supports, zero mismatches.

The same artifact reports graph and profile flags disabled in its own process.
FOLLOW-UP CORRECTION: the scheduled launcher forces both flags on. The earlier
inference of intentionally disabled live generation was wrong. Its graph check
skipped work using the wrong process context. Direct SQLite inspection adds the omitted
state: 14,541 completed discovery jobs, four completed synthesis jobs and one
blocked synthesis job with five attempts, last updated August 28 and error
synthesis_failed; all three retained observation claims are archived.
That is not live observation-precision evidence.

Profile run 3 completed August 30 at watermark 10184941/10184941, with 56 map
calls. Its stored model labels still say GLM. Current provider source and actual
Dreaming run records use Gemini; persisted labels do not establish the identity
of providers used across a resumed run.

## Remaining decisions / useful next work

| Finding | Next bounded action |
|---|---|
| Declared shadow versus actual application | ROADMAP and confirmed operator decision mm-c3f8 say shadow; task includes --apply-candidates and September 5 run records dry_run=0. Establish the controlling activation decision before changing that worker |
| Retained observation failure | The launcher enables graph work; one historical blocked job remains visible. The monitor must not hide it based on its own process flags |
| Benefit not established | No active observations; do not infer precision or retrieval benefit from completed discovery counts. Recent live discovery finds no components; investigate eligibility before further synthesis |
| Historical profile model labels | Attribute costs/providers using call-level records; a resumed run's initial labels are insufficient evidence |
| Release history lags metadata | CHANGELOG stops at 4.8.4 plus unreleased work while package source/runtime are 4.8.5. Reconstruct the actual tagged release before rewriting history |
| Transport receiver proof | Current target title is repaired; next natural scheduled invocation must prove delivery. Older router logs accepted a non-TUI composer; this shared Wezbridge behavior needs its owner's scoped review |
| Broader removals | Keep upstream adapters/tests until a usage or protection audit proves redundancy; no evidence here justifies deleting a backend or safety gate |

## Rollback

Revert the source commit to restore configuration/demo/docs behavior. Restore
the previous checkpoint message bodies from the operator's prior checkpoint
records if desired; cadence and scheduler definitions did not change. Restoring
the former empty tab title would make exact-title delivery fail again.

## Authorized follow-up: existing Dreaming history and Gemini retirement

The operator authorized fix, merge and deploy, selected existing Dreaming data
before further generation, and explicitly removed GLM from the active product.
Historical audit records and negative regression fixtures remain historical;
credentials and unrelated projects' provider installations are not removed.

### Measured history

Read-only SQLite scan at 2026-09-05T15:38:34Z, with an exact rolling seven-day
cutoff of 2026-08-29T15:38:34Z (julianday comparison, not mixed-format lexical
date comparisons). Earlier calendar-day preview counts are superseded.

| Measurement | Result |
|---|---|
| Runs | 33: 18 apply-mode ok, 14 apply-mode partial, 1 shadow ok |
| Provider calls | 274, all Gemini: 197 extraction ok / 10 errors; 58 consolidation ok / 9 errors |
| Recorded input/output tokens | 3,570,351 / 61,402; failed-call token usage is not known |
| Applied decisions | 88 add, 23 ignore, 2 propose_supersede, 3 reinforce |
| Distinct linked created claims | 93: 91 confirmed, 2 candidate; action count is not unique-claim count |
| Accessed linked claims | 17; access counters are not user-benefit attribution |
| Recently updated capture manifests | 197 captures, 139 candidates, 139 exact evidence quotes |
| Stored decision manifests | 186/186 complete unique candidate-ID sets, including empty sets |
| Application scope / linked existence | No mismatches or missing created-claim IDs in the window |
| Current sanitizer scan of recent persisted Dreaming claim text/object fields | No findings; this is not a whole-history secret audit |

An evenly spaced sample of 12 add actions was read with redaction, without a
provider call. Useful durable constraints coexist with transient state and
weak citations. In particular, mm-ed01~2's cited quote names a pane-identity
topic but does not support the claim's concrete implementation behavior;
mm-e3dc's quote does not support every hardware detail in its candidate.
These are citation-entailment concerns, not proof the full source contradicts
the claims. They require contextual review. No human precision label or
statistical quality estimate is fabricated from this diagnostic sample.

No matching human-labeled Dreaming evaluation corpus was found in the inspected
repository artifacts/reports. The existing evaluator requires 50 labeled
decisions and 20 human reviews. Existing history supplies material to evaluate
now; waiting for more raw runs will not supply missing ground truth.

### Implemented follow-up and verification

- Removed GLMConsolidator and its selector aliases; stale provider selection
  fails before a call. Generic OpenCode transport cannot re-enable retired
  provider identities or hide a retired model behind another provider prefix.
- Extracted the reused prompt and event parser instead of deleting unrelated
  adapters. Removed obsolete transport-only tests; retained prompt, duplicate
  decisions, malformed event, Gemini failure, and batching protection.
- Fixed readiness to check MEMORYMASTER_AGY_COMMAND, not OpenCode.
- The review-process flag no longer hides retained graph queue failures:
  blocked state is visible with WARN and worker activation explicitly unverified.
  Enabled-mode failures remain FAIL. No worker flag was silently changed.
- Seven fail-first regressions were observed (six provider/readiness failures,
  one false-PASS monitoring failure). Additional retired-model/provider aliases
  are covered. Combined focused suite: 166 passed in 22.15 seconds.
- A temporary verification environment installs candidate 4.8.6. Initial host
  tests correctly rejected source 4.8.6 versus installed 4.8.5 metadata; the
  isolated candidate environment resolves that mismatch without weakening tests.
- Ruff on changed modules passes. Integration and deployment evidence follows
  separately; no full-browser or full-project battle-tested verdict is claimed.

### Remaining findings

- Resolve the task's apply-candidates mode with the operator; the choice was
  requested while implementation continued. Historical claims remain untouched.
- The scheduled launcher enables graph/profile generation while the standalone
  operational check sees off flags. This discrepancy is now visible, not
  silently treated as a disabled-worker PASS.
- One retained blocked synthesis job is not reset or deleted; recent discovery
  has no eligible components. Further raw generation does not establish benefit.
- Provider cost in currency, statistical semantic precision, and causal recall
  benefit remain unmeasured. Existing retained data should be labeled and compared
  before commissioning more generation.

### Integration and deployment evidence

- 0547dfd (including prior eb8572e) fast-forwarded and pushed to origin/main.
- Installed wheel verified from outside the checkout with isolated imports:
  4.8.6, retired class absent, profile defaults aligned. Its public demo
  confirmed cited opt-in recall and retirement/staleness exclusion.
- General Python upgraded from 4.8.5; the actual scheduled-runtime virtual
  environment was still 4.8.4, despite the review measuring general Python.
  Both were installed as 4.8.6. Operational expected_version was updated.
- The managed HTTP task was restarted, without terminating any agent pane.
  Health and SQLite readiness returned 200; authenticated MCP tools/list
  returned 200 with 51 tools, including query_memory.
- Deployment log inspection exposed an old pythonw launcher import-order
  defect: logging handlers captured stderr=None before the log stream existed.
  A new fail-first test reproduced it. The repository template and installed
  launcher now import the HTTP service only after configuring streams.
  Five launcher/plugin tests pass. A fresh restart plus all three HTTP probes
  produced 956 log characters, zero logging errors and zero tracebacks.
- Eight long-lived stdio MCP processes predated installation. Their owning
  clients need reconnect/restart; no fleet-wide activation is claimed.
- Existing Dreaming mode and graph/profile launcher flags were preserved while
  the operator mode question remains unanswered. No historical memory cleanup,
  job reset, provider call, new scheduler, public release, or fabricated
  success watermark occurred during this follow-up.

### Completed bounded Dreaming assessment (subsequent review)

The operator selected review of all 88 recent add actions and a usefulness standard
that includes durable knowledge plus explicitly dated operational facts. This was
a read-only data assessment, not historical remediation or runtime reconfiguration.

- Reproduced the pinned seven-day interval ending 2026-09-05T15:38:34.710902Z:
  88 actions, 88 distinct numeric claim IDs, 74 cited messages; 86 claims confirmed
  and two candidate at the review read. Human IDs alone are not globally unique.
- All 88 quotes are exact and match the persisted excerpts after review redaction.
  Exact matching is not entailment: the AI assessment found 50 standalone excerpts
  fully supporting their claims and 70 full cited messages doing so.
- Potential value: 32 durable, 42 dated, six low-value, eight uncertain. These are
  AI diagnostic judgments, not human ground truth or automatic retention approvals.
- Verified concrete failures: a pending question became a preference; an older
  setting was captured despite a later change in the same session before ingestion;
  some completed facts cite the earlier request instead of the completion; temporary
  worktree names fragment project scopes; one confirmed personal-path form remains.
- Two Gemini-only calls completed (88-item review and 18-item challenge), reporting
  108424 input and 11645 output tokens. Currency cost and causal retrieval benefit
  remain unmeasured. No human-acceptance labels or feature-success watermark created.
- Privacy incident: an unlabeled numeric credential in adjacent raw context escaped
  the initial review scrubber and entered the first Gemini request. No value appears
  in the report. The local review filter now masks long numbers with a regression
  check, and the follow-up omitted adjacent context. No zero-disclosure claim is made.
- Local evidence is intentionally ignored/private under
  `artifacts/dreaming-review-20260905/`: REPORT.md, exact manifest, 88 annotations,
  both Gemini assessments, and a read-only verifier. Its coverage checks pass;
  missing rows, duplicate rows, fake human labels and disabled numeric redaction
  are each rejected by inverse controls. The evidence is not published with source.
- No claims, jobs, scheduler modes or running clients were changed during this
  assessment. The next bounded fixes are entailment/modality, same-session temporal
  reconciliation, canonical scope assignment and context/privacy boundaries.

### Source-steward rollout preflight and installed-hook reconciliation

The operator explicitly authorized merge and deployment of current MemoryMaster
work. PR #251 is the sole open project PR. Unrelated local work remains excluded.

- The source candidate is `cf494e7`, version 4.8.7. Its complete local Windows
  non-ML run passed: 4,946 passed, 77 skipped, one expected failure, 90 ML items
  deselected. The independent CI ML job passed 89 tests with six skips.
- Two earlier runs exposed stale test setup, not an exemption from the new
  guard: the historical-correction fixture now seeds an already-confirmed row;
  the tiny steward-CAS fixture now includes source identity and citation lineage.
  Original historical-correction and concurrent-writer assertions remain intact.
- The built wheel's 398 package files match the reviewed source byte-for-byte.
  Wheel SHA-256: `03b4e04b6dca42f6e042b87ff0ab53935d29c1f0e670028de734948a0573d0de`.
  A clean isolated installation passes the public lifecycle demo, MCP stdio
  initialization/tool listing and missing-review/accepted-review/replay smoke.
- Strict OSV audits passed for project dependencies and the clean installation.
  The latter required upgrading its newly-created environment's old pip first.
  The existing dedicated production environment passes pip check; the shared
  system environment has unrelated dependency conflicts and was not repaired.
- The release diff has zero Gitleaks findings. The broader local Git-history
  scan has 51 detector hits: 44 exact previously reviewed fixture fingerprints,
  two newer synthetic workflow-test fixtures and five findings in an old unmerged
  WIP snapshot. This is not a clean whole-history/supply-chain verdict, nor a
  count of confirmed live credentials. No history rewrite or rotation occurred.
- An SQLite-API backup was restored to a separate SSD file and fully checked:
  7,416,750,080 bytes, integrity `ok`, zero FK violations, 148,039 claims,
  154,111 citations and 2,768,224 events. Backup and restored SHA-256 both equal
  `35d5e9972cfde5c17fa85d4a525b0cbbddaa513662ff2b79645473bde2e19dfa`.
  The first integrity scan on the slower backup drive was stopped; the completed
  restore-side scan and exact hash comparison are the verification evidence.
- Rehydrating blocked observation job 8924 found zero eligible support rows;
  all eight supporting claims were retired. A bounded transaction revalidated
  this and cancelled that job with audit event 2768452, preserving its five
  attempts and original failure. No supporting claim was modified or deleted.
  The subsequent read-only review reports no active backlog or expired leases,
  52 profile facts with 565 supports and zero manifest mismatches; canary rank 4.
- A deployment inspection found the installed steward hook still hardcoded
  Claude/Ollama and used an obsolete direct-SQL archival block. After backing up
  the hook, local configuration was changed to Google Gemini 3.5 Flash Lite,
  no cross-provider fallback, and the existing scheduled_archive lifecycle job.
  The approved dedupe/curation modes and six-hour cadence were preserved.
- Existing per-cycle budget guards now cap that steward API route at 20 calls,
  50,000 estimated tokens and two provider failures. Read-only AST checks failed
  before the changes and pass afterward; eight existing archival/runtime tests
  pass. One synthetic Gemini API request succeeded without reading memory data.
- These hook corrections are installation-specific; replacing the hook with a
  generic setup template requires revalidating provider choice and local policy.
  Reproducible checks and private artifacts remain in the implementation
  worktree's ignored `artifacts/deploy-4.8.7/` directory.
- The daily checkpoint's September 5 exit 4 means no exact project/title target
  existed at fire time. The current memorymaster tab is discoverable, but the
  next natural delivery is unverified. Existing long-lived stdio MCP processes
  still require reconnection after package deployment; they are not terminated.

### Completed local deployment of 4.8.7

- PR #251 merged to main as `4ffc3003aabbe466cb954bdbbd613d4eed19ab02` at
  2026-09-05T23:07:50Z. CI run 33995345529 passed the six platform/Python jobs,
  ML, performance and deployment smoke. Performance artifact: ingest p95
  0.0810 seconds, query p95 0.0504 seconds, cycle p95 1.9359 seconds.
- Caveat discovered by reading CI evidence: the optional legacy eval job's
  continue-on-error masks missing `benchmarks/cases.jsonl`,
  `benchmarks/cases_general.jsonl` and `benchmarks/cases_adversarial.jsonl`.
  No eval artifact exists. Its nominal success is not evaluation evidence.
- Installed the exact verified 4.8.7 wheel into both existing Python targets,
  without changing unrelated application dependencies. Refreshed the primary
  checkout's generated package metadata to avoid source/installed ambiguity.
  A 4.8.6 rollback wheel and the pre-change installed hook are retained privately.
- Restarted only the managed HTTP task. Listener PID changed from 43324 to 74296.
  Health/readiness return 200, unauthenticated MCP returns 401, authenticated
  initialize/tools-list succeeds with 51 tools. Post-restart log inspection
  found startup complete and zero ERROR/Traceback/CRITICAL entries.
- Both freshly launched installed runtimes passed missing-review rejection,
  reviewed candidate promotion and unique replay receipt checks on disposable
  databases. No mutating steward cycle was run against the authoritative DB.
- The dedicated runtime passes pip check and strict OSV audit. Its installer
  tooling was upgraded from pip 24.0 to 26.2.1; unrelated shared-Python conflicts
  remain outside this deployment's scope.
- Updated operational-review expected version to 4.8.7. Its real read-only run
  at 2026-09-05T23:10:51Z returned PASS/exit 0: SQLite quick_check ok, FK 0,
  migration 24; no pending/leased/retryable/blocked jobs or expired leases;
  52 profile facts, 565 supports, zero mismatches; retrieval canary rank 4.
  The wrapper recorded review_performed=true only after those checks finished.
- That review still explicitly says worker activation is unverified from its
  own process flags. The Dreaming launcher enables graph/profile work, but the
  next natural source-aware worker cycle and statistical quality remain follow-up
  evidence, not outcomes fabricated from installation. Existing peer MCP clients
  require reconnection; no panes or their stdio processes were killed.
- This is merged source and verified local deployment, not a new GitHub release
  or PyPI publication. Historical claims, retained raw evidence, dormant workflow
  activation and unrelated `delta-exchange/` work were not rewritten or enabled.
