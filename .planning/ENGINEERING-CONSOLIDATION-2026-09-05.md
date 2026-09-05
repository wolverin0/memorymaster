<!-- doc-head: September 5 consolidation, Gemini retirement, historical Dreaming evaluation and deployment -->
# Engineering consolidation: 2026-09-05
Covers: instructions, status reconciliation, profile defaults, lifecycle demo and checkpoint delivery.
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
