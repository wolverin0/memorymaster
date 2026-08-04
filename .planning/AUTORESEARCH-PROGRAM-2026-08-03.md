# MemoryMaster autoresearch program
# Covers: the measured multi-phase improvement loop following the vNext implementation.
# Key terms: LongMemEval, recall latency, retrieval quality, graph quality, capture reliability.
# Read when: launching, monitoring, or deciding whether an autoresearch phase is complete.
# Authority: implements ROADMAP.md quality gates; it does not replace the product roadmap.
# Safety: temporary SQLite and deterministic providers only; no live DB, scheduler, push, PR, or publish.
# Updated: 2026-08-04 after local convergence passed; authenticated image scanning remains external.

## Objective

Use bounded, mechanically verified experiments to make MemoryMaster faster and
more accurate without weakening governed claims, trusted-recall defaults,
scope isolation, citations, lifecycle rules, or provider budgets.

Each row is a separate run. Reaching one row's target completes only that row;
it never terminates the entire program.

## Program

| Phase | Primary metric | Non-regression gates | Status |
|---|---|---|---|
| 1. Benchmark iteration speed | LongMemEval-S 25-question elapsed seconds | Exact rankings, R@5/R@10/MRR, 0 provider calls | **Complete**: 105.168s -> 66.569s (`7a41390`) |
| 2. Production-path performance | Temporary-SQLite query p95 | 0 misses; confirmed count; ingest/cycle SLOs | **Complete**: 52.265ms -> 38.608ms (`544048b`); one 44.624ms drift run remains recorded |
| 3. Retrieval quality | LongMemEval-S MRR | R@5/R@10 no regression; held-out/full gate; provider-call cap | **Complete**: full MRR 0.9021 -> 0.9076; R@5 0.966 -> 0.972; R@10 0.984; 0 provider calls (`8f2caf6`) |
| 4. Graph-supported retrieval | Graph-focused top-5 hit rate | Authorized active supports only; 0 cross-scope results | **Complete**: 0/6 -> 6/6; 0 forbidden hits/provider calls; full LongMemEval unchanged (`1b4f5f2`) |
| 5. Capture performance | Capture acknowledgement p95 | 0 duplicates/orphans/secrets; terminal job states | **Complete at baseline**: worst of five p95 runs 107.0ms vs 500ms SLO; 0 integrity failures; 62 passed, 1 optional-parser skip (`0dd80de`) |
| 6. Convergence | Gate pass count | Full tests, Ruff, diff check, package validation | **Complete locally**: 4,250 non-ML and 97 ML tests passed; wheel, SBOM, dependency, history-secret, migration, restore, demo, and package gates passed; Docker Scout authentication remains external |

## Execution contract

- Run one focused hypothesis per iteration and log it before starting another.
- Use development slices for iteration and a held-out/full gate before keeping a
  production change.
- Prefer production-path metrics. Harness-only wins are labeled as tooling and
  cannot be reported as product speedups.
- Keep local experiment commits on the isolated worktree branch. Publication,
  PR creation, live activation, and scheduler changes remain separately gated.
- The coordinating chat remains open while phases run. Detached runtime
  completion is not treated as user notification; terminal status must be
  polled and reported here before the coordinator yields.

## Stop conditions

The bounded six-phase program is complete. Release readiness remains gated by
an authenticated Docker Scout image scan, comparable full OAuth-backed QA, a
statistically meaningful private capture/graph corpus, and operator approval
for any public push or PR. A per-phase numeric target never ended the program
before the later phases ran.

## Convergence evidence

- Collection reports 4,419 tests.
- The isolated non-ML suite passed 4,250 tests with 71 skips, 97 deselections,
  and one expected xfail; the isolated ML suite passed 97 tests.
- Ruff, generated release truth, and `git diff --check` passed.
- Disposable SQLite migration, restore, lifecycle, and demo coverage passed 61
  focused tests. PostgreSQL was waived by the operator.
- Default and `capture` wheel installs passed from an empty working directory;
  package contents and the public-v1 release contracts passed.
- Strict OSV project and release-extra audits, reviewed full-history Gitleaks,
  and artifact-bound CycloneDX validation passed.
- The fail-closed supply-chain runner passed four checks and stopped only when
  Docker Scout required an authenticated Docker session. No credentials were
  requested, no result was waived, and no public or live runtime state changed.

The detailed evidence and remaining gates are recorded in
`.planning/audits/2026-08-04-autoresearch-convergence/audit-delta.md`.
