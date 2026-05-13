---
name: memorymaster-release-discipline
description: Cut a memorymaster release that introduces or modifies recall/ranking/scoring behavior — apply measurement-first methodology and honest-null acceptance. Use BEFORE shipping any change that touches the recall pipeline, ranker, query_memory, or scoring weights. Survey adjacent tools, measure precision@k against ≥953-prompt eval, ship null/harmful results honestly, avoid cloud /schedule for local-DB tasks. Encodes the v3.5–v3.13 release pattern.
when-to-use: User proposes or starts work on a new recall/ranking/scoring feature, or asks to ship a release whose changeset touches `query_memory`, recall weights (W_LEXICAL, W_FRESHNESS, W_GRAPH, etc.), scoring functions, or the ranker. Do NOT fire on feature-heavy / docs / hotfix releases that don't touch recall behavior (e.g. v3.14.0's 32-PR session was correctly out of scope).
allowed-tools: [Bash, Read, Grep, Edit]
---

# memorymaster-release-discipline

## Overview

memorymaster's release cadence (v3.5–v3.13, spanning 2026-04-26 to 2026-04-30) established a discipline that treats every release as an experiment: measurement-first methodology, honest-null acceptance, survey-and-steal feature scouting, dead-code salvage when a release ships work that turns out to be wired wrong. This skill encodes that discipline so any future v3.X cut follows the same pattern.

## When to use

- Bumping the version for any new feature, optimization, or "improvement"
- Proposing a new recall/ranking feature (the measurement gate fires automatically)
- Following up a release that turned out to leave dead code in the codebase (salvage flow)
- Considering scheduled/automated tasks against memorymaster.db

## When NOT to use

- Pure docs-only releases that don't touch recall, ranking, scoring, or hook behavior — those need a `git commit` and a version bump, no measurement work
- Hotfix releases with a single clear bug fix and no methodology change
- Releases of memorymaster-adjacent tools (graphify, GitNexus) — those have their own disciplines

## Inputs

Before invoking:
- The new feature/change you want to release
- Access to the eval prompt set (current canonical: N=953 prompts per v3.12.0)
- A clean origin/main baseline to measure against

## Procedure

### 1. Frame the release as a measurement, not a feature

State up-front: **"This release tests hypothesis H against baseline B; we ship the result regardless of whether H wins."** Honest-null is the default; "feature works" must be earned.

Past anchors:
- v3.6.0 (mm-d5a2) — null release: W_LEXICAL/W_FRESHNESS/W_GRAPH defaults proven at-or-near optimal after 5h compute. Shipped the negative finding.
- v3.10.0 (mm-3f8d) — actively harmful release: F6 closets caused −0.018 to −0.044 precision@5 vs baseline; F1/F5/F8 null deltas. Shipped the harm finding and the rollback.

### 2. Survey adjacent tools BEFORE designing the feature

Before writing any new ranking/recall code, scan the memory-tool landscape for portable features. v3.9.0 (mm-6122) surveyed 6 tools (gbrain, MemPalace, graphify, claude-mem, GitNexus, My-Brain-Is-Full-Crew) and ported 9 features in one release. Pattern:

- For each candidate tool, identify ≤3 features worth porting (claim_type ranking, alias normalization, etc.)
- Tag each port with the source tool's name (F1/F2/F3...) so post-release attribution survives
- Ship the survey doc alongside the release in `artifacts/steal-from-others-<date>.md`

### 3. Run the eval at the canonical scale

Current canonical: **N=953 prompts, top-50 candidates** (v3.12.0 mm-67ff). Smaller evals (N=100, top-15) produce false signal — v3.5.2 → v3.12.0 history shows.

Methodology:
- Re-label GT against top-50 candidates via parallel haiku subagents (5-way fan-out)
- Compute precision@5 baseline before merging any change
- Compute precision@5 with the change
- Delta < ±0.001 → null finding (ship honestly)
- Negative delta → harm finding (ship the rollback and document the reason)

### 4. Audit + autoresearch B1 the release

Before tagging, run the v3.5.2 audit pattern (mm-c693):
- Add tests for any new provider/hook code (e.g., 11 tests for claude_cli provider in v3.5.2)
- Regression tests for fix-pinning (asserts the shipped template uses the expected env-assignment pattern, not the buggy one)
- Sync any hook/scheduler templates to the new version

### 5. Tag, release, and document the release in the changelog

Format follows v3.6/v3.9/v3.10 (Keep a Changelog loose). Include:
- The hypothesis tested
- The eval scale (N=, top-k=)
- The delta (null / +0.XXX / −0.XXX)
- Which features (F1/F5/F8) deltas attribute to
- Any rollback or salvage flagged for the next release

### 6. Salvage dead code in the NEXT release if needed

If the just-shipped release left dead code (a feature stub merged but not wired, or a new module unreachable from the call graph), open a salvage PR in the next minor version. v3.13.1 (mm-6eba) is the canonical example: PR #5 wired `candidate_dedupe.run(store)` between extractor and deterministic stages, and added the `dedupe` key to `MemoryService.run_cycle`'s result dict. The salvage release should:
- Add the missing wiring with explicit tests
- Update CHANGELOG to note "salvage of <prior>" with a link to the dead-code claim
- NOT add new features in the same PR (single-concern rule)

## Failure modes

### Skipping measurement and shipping a "feature"

Without N=953 measurement, you don't know if the feature helps, hurts, or is null. v3.5–v3.10 history shows the eval routinely surfaces features that look correct in code review but degrade precision@5 in practice. **Always measure before shipping recall/ranking changes.**

### Scheduling memorymaster.db work as cloud routines

CONSTRAINT (mm-0552): do NOT propose Anthropic Cloud `/schedule` remote-agent routines for tasks that read or write the local `memorymaster.db` SQLite file. Cloud runners spin up isolated sandboxes with no access to user's local files, env vars, or installed hooks. Audit, precision-check, dedupe — all of these are local-only tasks. Schedule them via Windows Task Scheduler / cron on the user's machine, not via Anthropic Cloud.

### Flaky Windows CI tests blocking merge

GOTCHA (mm-79a3~3): two known intermittent failures on the Windows matrix slot — `tests/test_sqlite_core.py::test_support_email_update_pr...` and one other. Both pre-exist any v3.13.x work and pass on retry. Rule: if Windows CI fails with these specific tests, re-run the workflow once before declaring a regression. Do not write fix commits without independently reproducing the failure deterministically.

### Inflated docs in README

If a release's README crosses ~500 lines, factor the operator-level depth (hooks, dashboard, steward, dream bridge, wiki engine, entity registry, OpenClaw/GitNexus, troubleshooting, performance SLOs, one-prompt agent install) out to `docs/handbook/`. v3.5.1 (mm-8079) did this: 890 → 187 lines, -79%, no code change. Slim README = better discoverability; deep docs live in the handbook.

## Verification

After release tag:

```bash
git log --oneline -1                                     # confirm release commit landed
gh release view v<X.Y.Z>                                 # confirm GitHub release exists
python -m pytest tests/ -q                               # full suite green
python scripts/eval_recall_precision_at_5.py --n 953 --top-k 50  # if methodology change, re-run
```

Then update CHANGELOG.md with the eval delta line and link to the relevant claim IDs.

## References

- mm-8079 — DECISION: v3.5.1 docs reorg pattern (slim README, deep handbook)
- mm-c693 — DECISION: v3.5.2 audit + autoresearch B1 pattern
- mm-d5a2 — DECISION: v3.6.0 honest-null release with weight optimality
- mm-6122 — DECISION: v3.9.0 "Steal Everything Good" survey-and-port pattern
- mm-3f8d — DECISION: v3.10.0 honest-null AND honest-harm release (F6 closets rolled back)
- mm-67ff — DECISION: v3.12.0 measurement methodology (N=953, top-50)
- mm-6eba — DECISION: v3.13.1 dead-code salvage pattern (PR #5)
- mm-0552 — CONSTRAINT: no cloud /schedule for memorymaster.db tasks
- mm-79a3~3 — GOTCHA: known flaky Windows CI tests, retry-safe
- This skill generated by `/skillify --discover` on 2026-05-12 from a cluster of 17 claims in `project:memorymaster` (score 0.53).
