<!-- doc-head: September 5 bounded engineering consolidation and measured gaps -->
# Engineering consolidation: 2026-09-05
Covers: instructions, status reconciliation, profile defaults, lifecycle demo and checkpoint delivery.
Key terms: source versus runtime, public retirement, disabled generation, shadow discrepancy.
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

The same artifact records graph and profile generation intentionally disabled.
Its graph check skips disabled work. Direct SQLite inspection adds the omitted
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
| Dormant observation failure | Keep the blocked job visible while generation is off; investigate the synthesis failure only if that feature is selected for renewed evaluation |
| Benefit not established | No active observations; do not infer precision or retrieval benefit from completed discovery counts. Measure an eligible sample when generation is deliberately enabled |
| Historical profile model labels | Attribute costs/providers using call-level records; a resumed run's initial labels are insufficient evidence |
| Release history lags metadata | CHANGELOG stops at 4.8.4 plus unreleased work while package source/runtime are 4.8.5. Reconstruct the actual tagged release before rewriting history |
| Transport receiver proof | Current target title is repaired; next natural scheduled invocation must prove delivery. Older router logs accepted a non-TUI composer; this shared Wezbridge behavior needs its owner's scoped review |
| Broader removals | Keep upstream adapters/tests until a usage or protection audit proves redundancy; no evidence here justifies deleting a backend or safety gate |

## Rollback

Revert the source commit to restore configuration/demo/docs behavior. Restore
the previous checkpoint message bodies from the operator's prior checkpoint
records if desired; cadence and scheduler definitions did not change. Restoring
the former empty tab title would make exact-title delivery fail again.
