# Phase 4 Budget Audit Delta — 2026-07-14

## Scope and outcome

This targeted delta reconciles MM-OPS-03, MM-UX-02, MM-UX-03,
MM-MAINT-01, MM-MAINT-02, MM-INTEGRITY-01, and MM-TEST-01. It is not a
new 13-domain audit.

The first independent Phase 4 review discovered one reproducible High
governance race: the dashboard displayed proposal event N but submitted only
the claim ID, allowing a newer proposal to be resolved instead. Two
adversarial tests witnessed the failure. The bounded correction binds every
action to the exact displayed `proposal_event_id` and rejects missing,
malformed, or stale identifiers. The correction boundary passed 54 tests; the
final strict malformed-ID re-review passed 18 focused dashboard tests.

The correction review found no remaining reproducible Critical/High regression
introduced by Phase 4. The latest same-scope audit contains zero new findings.

| Finding set | Delta |
|---|---|
| MM-MAINT-02 | RESOLVED — explicit companion boundaries, removed dead plugin seam, smaller real services, ratcheting budgets, dated shim gate |
| MM-UX-02..03 | RESOLVED — truthful pending/error/retry states, exact proposal-event binding, inspectable evidence, browser/a11y/mobile acceptance |
| MM-OPS-03 | RESOLVED in repository — publish consumes only verified bytes; an actual tag/PyPI run remains BLOCKED-EXTERNAL |
| MM-MAINT-01, MM-INTEGRITY-01, MM-TEST-01 | RESOLVED — generated release truth, canonical version, clean import probe, deterministic test contracts |
| P4-CORR-01 | FIXED — exact proposal-selection race discovered during convergence |

## Audit-loop reconciliation

```text
AUDIT LOOP ITERATION 2

Previous report: .planning/audits/2026-07-10-baseline/audit-report.md
Current report:  .planning/audits/2026-07-14-phase4-budget-delta/audit-delta.md
Roadmap:         .planning/REMEDIATION-OPTIMIZATION-PLAN-2026-07-10.md

Delta:
  Fixed:            8
  Unchanged:        8 valid BLOCKED-EXTERNAL canonical findings
  Severity changed: 0
  New findings:     0

Blocked:
  MM-SEC-01, MM-SEC-02, MM-OPS-01, MM-SEC-03, MM-OPS-04,
  MM-PRIV-01, MM-PRIV-02, MM-OPS-05 — BLOCKED-EXTERNAL
  Gate file: external-actions-required.md

Convergence: READY for local release-candidate review; NOT production approval
```

The race was caught by the independent phase audit before convergence, so it
does not represent a blind spot in the latest audit method. The permanent
adversarial acceptance tests are the regression guard.

## Verification evidence

| Gate | Evidence |
|---|---|
| Collection/generated truth | 4,221 tests; 36 MCP tools; 106 main CLI commands; 5 ops commands; 8 entrypoints |
| Full non-ML, once | 4,053 passed, 70 skipped, 95 deselected, 1 xfailed, 2 warnings in 919.40s |
| ML, isolated | 95 passed, 4,126 deselected, 1 warning in 18.45s |
| Bounded correction | Two witnessed RED tests; 54 passed after correction; strict malformed-ID re-review 18 passed |
| Browser/a11y | Headless Chromium governance failure/retry, exact event, narrow layout, and visible-error coverage passed |
| Static quality | Project Ruff, changed-file Ruff, syntax/import checks, inline JavaScript parse, generated-truth check, and `git diff --check` passed |
| Wheel/sdist | Build/install passed; isolated import reports 4.4.1 from venv `site-packages`; CLI and MCP entrypoints passed |
| Disposable runtime | Compose config passed; local image built; fresh temporary DB initialized; health `ok`, readiness `ready`, runtime 4.4.1; MCP entrypoint passed |

## External evidence retained

Authenticated/TLS Qdrant, two-role disposable Postgres, approved immutable
release images and image scans, disposable Kubernetes/Helm, dependency audit,
history-secret classification, off-device/Postgres recovery, legal/privacy
decisions, and an actual tag/PyPI publication remain external. No live data,
paid provider, production service, credential, or external configuration was
mutated.
