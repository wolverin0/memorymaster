# MemoryMaster autoresearch program
# Covers: the measured multi-phase improvement loop following the vNext implementation.
# Key terms: LongMemEval, recall latency, retrieval quality, graph quality, capture reliability.
# Read when: launching, monitoring, or deciding whether an autoresearch phase is complete.
# Authority: implements ROADMAP.md quality gates; it does not replace the product roadmap.
# Safety: temporary SQLite and deterministic providers only; no live DB, scheduler, push, PR, or publish.
# Updated: 2026-08-04 after retrieval quality passed dev, held-out, and full-corpus gates.

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
| 4. Graph-supported retrieval | Graph-focused top-5 hit rate | Authorized active supports only; 0 cross-scope results | **Next** |
| 5. Capture performance | Capture acknowledgement p95 | 0 duplicates/orphans/secrets; terminal job states | Pending |
| 6. Convergence | Gate pass count | Full tests, Ruff, diff check, package validation | Pending |

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

The program stops only when all phases are complete, the operator interrupts,
or a phase reaches a documented hard/soft blocker. A per-phase numeric target
is not a program-wide stop condition.
