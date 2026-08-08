# MemoryMaster paper-research implementation ledger - 2026-08-08
# Covers: executable PPR-1 through PPR-6 work derived from the governed paper radar.
# Key terms: representation evaluation, sustainability telemetry, budget policy, evidence rehydration, temporal projection, skill outcomes.
# Read when: implementing or verifying research-derived MemoryMaster changes after the primary-paper review.
# Authority: subordinate to ROADMAP.md and PAPER-RADAR-REVIEW-2026-08-08.md; it does not create product scope.
# Safety: SQLite-only, temporary/synthetic evaluation first, no live activation, automatic promotion, or public release.
# Updated: 2026-08-08 after PPR-2 GREEN; PPR-3 through PPR-6 remain queued.

## Execution status

| Package | Status | Deliverable | Acceptance boundary |
|---|---|---|---|
| PPR-1 | COMPLETE | Versioned synthetic representation and active-use evaluator | Deterministic scorer, five-profile matrix contract, failure attribution, tests, and no provider calls |
| PPR-2 | COMPLETE | Stage-level sustainability observations | Aggregate-safe timing, content-read, provider, token, cache, tier, fallback, and correctness fields |
| PPR-3 | QUEUED | Explicit deterministic retrieval budgets | Low/balanced/high/temporal/procedural policies evaluated in shadow mode before runtime adoption |
| PPR-4 | QUEUED | Progressive claim-to-evidence rehydration | Scope-filtered supported paths, exact evidence map-back, bounded sufficiency checks, diagnostic fallback |
| PPR-5 | QUEUED | Temporal and episode projections | Rebuildable occurrence/interval/current/adjacent-evidence projections with atomic citations preserved |
| PPR-6 | QUEUED | Outcome-aware governed skills | Success/failure/ambiguous evidence without automatic confirmation, reinforcement, rewrite, or archival |

## Fixed sequence

1. Build PPR-1 before changing retrieval or memory representations.
2. Add PPR-2 so every later experiment records quality and resource cost together.
3. Evaluate PPR-3 in shadow mode; keep the current governed recall path as the control.
4. Implement PPR-4 only after the scorer can distinguish retrieval from use and citation failures.
5. Implement PPR-5 only after temporal and supersession adversarial fixtures are green.
6. Implement PPR-6 last because execution outcomes affect governed skill review signals.

## PPR-1 acceptance criteria

- A publishable, explicitly synthetic, versioned JSONL corpus covers latest versus superseded state, occurrence versus dialogue time, validity intervals, durative state, affect, narrative arcs, and exact tool parameters.
- Predictions are evaluated for the fixed matrix: `claims-only`, `evidence-only`, `claims+evidence`, `claims+approved-skills`, and `claims+ephemeral-guidance`.
- Answer correctness and citation correctness are independent metrics.
- Tool scoring distinguishes explicit, default, inferred, and missing parameters and requires exact structured values.
- Case diagnostics attribute retrieval miss, retrieved-but-unused, hallucinated default, lossless-retention failure, wrong tool, answer error, argument error, and citation error.
- Reports contain aggregate metrics and case IDs/booleans, never fixture source text or answers.
- The harness is deterministic, performs no provider call, touches no live database, and has at least 80% focused coverage.
- Existing LongMemEval R@5/MRR and full-QA gates remain unchanged; running those expensive gates is an integration-boundary action, not part of every local scorer test.

## Evidence log

| Date | Package | Evidence | Disposition |
|---|---|---|---|
| 2026-08-08 | PPR-1 | RED: evaluator import failed before implementation. GREEN: 15 focused tests pass; 86% focused coverage; eight mutation cases produce the required distinct failures. | Implemented |
| 2026-08-08 | PPR-1 | Compatibility: 31 evaluation tests pass; Ruff and `git diff --check` pass; 4,551 tests collect. Full suite remained active but exceeded the 15-minute local ceiling, so it is not recorded as passing. | Verified with explicit full-suite timeout |
| 2026-08-08 | PPR-1 | Runtime boundary: deterministic temporary-file CLI test passed; no provider call, database open, scheduler change, live capture, or public publication occurred. | Safe offline completion |
| 2026-08-08 | PPR-2 | RED: sustainability module import failed before implementation. GREEN: 7 tests pass with 92% focused coverage; 54 evaluation/planner/packing/architecture tests pass. | Implemented and verified |
| 2026-08-08 | PPR-2 | Disposable SQLite integration retrieves a confirmed scoped claim through `MemoryService.retrieve`, packs it with the production context packer, emits retrieval/packing stages, and records zero provider calls without query or claim text in the artifact. | Authoritative evaluation path wired |
| 2026-08-08 | PPR-2 | Strict enums cover retrieval, graph expansion, evidence map-back, admission, packing, skill recall/review, answer generation, and judge generation; per-request observations are capped at 64. | Aggregate-safe contract complete |

## Activation and rollback

This ledger authorizes implementation and disposable evaluation only. It does
not authorize live database writes, scheduled-task changes, a merge, a push, a
release, or public publication. New evaluation files roll back by reverting the
atomic package commit; later runtime packages must document their own flags and
data rollback boundaries before activation.
