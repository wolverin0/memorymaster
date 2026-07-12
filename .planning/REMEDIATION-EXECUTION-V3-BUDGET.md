# MemoryMaster Phase 1 — Budget Execution V3

**Created:** 2026-07-12

**Supersedes for execution:** `REMEDIATION-EXECUTION-V2.md`

**Does not replace audit evidence:** the original remediation plan and ledger remain historical sources of truth.

## Objective

Finish Phase 1 security convergence without continuing the open-ended
fix/test/review loop. Preserve completed work, close the two remaining Wave B
packages, run the expensive gates once, produce a targeted Phase 1 audit delta,
and stop before Phases 2–4.

## Authoritative inputs

- Scheduler: `.planning/REMEDIATION-EXECUTION-V3-BUDGET.md`
- Audit roadmap: `.planning/REMEDIATION-OPTIMIZATION-PLAN-2026-07-10.md`
- Finding ledger: `.planning/audit-remediation-ledger.md`
- Baseline audit: `.planning/audits/2026-07-10-baseline/`

When V3 conflicts with V2 on execution frequency, review loops, or completion
criteria, V3 controls. V3 does not erase findings or imply that Phases 2–4 are
complete.

## Resume checkpoint

Integration worktree:
`G:\tmp\memorymaster-remediation-20260710`

Completed Phase 1 packages include R1.1–R1.3 and R1.4 repository work. Recent
commits include:

- `a3e3824` — bridge persistence transport
- `702b59d` — legacy sensitivity inventory
- `a858419` — private authenticated deployment defaults

Remaining code packages:

1. **Supply-chain package**, currently uncommitted in the integration worktree:
   `.dockerignore`, `docs/security_supply_chain.md`,
   `scripts/run_supply_chain_checks.py`, `scripts/validate_sbom.py`, and
   `tests/test_supply_chain_contracts.py`. Its focused suite last passed 65
   tests. A real unsuppressed history scan failed closed with 40 potential
   findings across 10 commits; this is external review/rotation evidence, not a
   repository test failure to suppress.
2. **Qdrant transport package**, uncommitted in
   `G:\tmp\memorymaster-remediation-agents\r15-qdrant`. Worker evidence: 22
   transport adversarial tests, 105 focused non-ML tests, and 28 ML tests
   passed. Independent final review and integration remain.

The authorized read-only legacy inventory completed without changing the live
DB but returned `BLOCKED`: SQLite scanned 4,908,349 records and flagged 223,230,
with one unscannable value; artifacts scanned 1,184 files, flagged 654, and had
270 unscannable plus 2 truncated files; spool completed; Qdrant was
`BLOCKED-EXTERNAL/qdrant_not_configured`. Cleanup/redaction remains forbidden.

## Budget rules

1. Freeze scope. Do not add features, broad refactors, release automation, or
   general hardening outside the remaining Phase 1 findings.
2. Fix only a reproducible Critical/High security or integrity defect in code
   changed by Phase 1. Record Medium/Low observations in the backlog unless a
   correction is trivial and required for a passing final gate.
3. Do not require a new adversarial RED for every small correction. Existing
   witnessed REDs remain evidence. Add a new test only for a newly discovered
   Critical/High bypass that lacks coverage.
4. Do not rerun the full suite after individual edits. Use syntax/import checks
   while editing, one focused package run before its commit, and the full
   verification ladder once after both packages are integrated.
5. Use at most one child beside root. Use one Sol/high read-only security review
   at the combined integration boundary; time-box it to the changed Phase 1
   files. Do not fan out another full audit.
6. Allow one correction batch after the final review. If it reports only
   Medium/Low or out-of-scope work, backlog it and continue.
7. Classify unavailable services, credentials, images, scanners, or live-data
   actions as `BLOCKED-EXTERNAL` immediately. Do not wait for them and do not
   manufacture passing evidence.
8. Preserve the main checkout and all unrelated dirty files. Do not push,
   publish, deploy, rotate credentials, rewrite Git history, or mutate product
   data.

## Execution sequence

### Checkpoint 1 — Close supply-chain package

- Review the existing five-file diff; do not expand its scope.
- Run its focused tests, Ruff, format check, and diff check once.
- Preserve the real Gitleaks failure as `BLOCKED-EXTERNAL` with aggregate-only
  evidence.
- Run GitNexus change detection and make one atomic conventional commit.

### Checkpoint 2 — Integrate Qdrant transport

- Review the frozen worker diff and run one Sol/high read-only review covering
  only credential scoping, CA/TLS verification, secret-free errors, and R1.3
  quarantine preservation.
- Fix only reproducible Critical/High issues in one batch.
- Run the worker's focused non-ML and required ML gates once, then integrate and
  make one atomic conventional commit.

### Checkpoint 3 — One final verification boundary

Run these once on the final integrated commit:

1. `ruff check memorymaster/` plus changed scripts/tests.
2. Combined R1.4/R1.5 targeted adversarial and integration tests.
3. `python -m pytest tests/ -q --tb=short -m "not ml"` once.
4. Test collection once and compare with the prior Phase 1 count.
5. Required Qdrant ML tests once.
6. Compose fail-closed/private-binding configuration checks once.
7. Clean-wheel build/install and SBOM-to-wheel validation once.
8. Dependency/history/image scanners when locally available; otherwise record
   exact blockers. Do not build or pull images solely to satisfy this goal.
9. SQLite/Postgres/Qdrant external parity remains blocked unless disposable
   services are already available.
10. Run GitNexus change detection before each commit and preserve embeddings
    when refreshing the index.

Do not run browser/a11y, all 13 audit domains, performance benchmarks, recovery
drills, deployment smoke, or Phases 2–4 tests under this goal unless a changed
Phase 1 file directly breaks them.

### Checkpoint 4 — Targeted audit delta and stop

Reconcile only the Phase 1 findings and hard stops:

- `MM-SEC-01`
- `MM-SEC-02` (Phase 1 containment only; R2.1 remains backlog)
- `MM-SEC-03`
- `MM-SEC-04`
- `MM-OPS-01`
- `MM-OPS-04` (Phase 1 defaults only; R3.4 remains backlog)

Produce one concise audit delta with commit/runtime evidence, external actions,
rollback notes, and explicit Phase 2–4 backlog boundaries. Do not rerun the
full 13-domain audit, blind-spot catalog, or an unbounded convergence loop.

## Completion contract

Phase 1 is complete for this budget goal when:

- The two remaining repository packages are committed and their focused gates
  pass.
- The one final verification boundary is green or honestly
  `BLOCKED-EXTERNAL`.
- No reproducible unresolved Critical/High regression introduced by the Phase
  1 branch remains.
- Medium/Low and out-of-scope findings are recorded as backlog rather than
  expanded into new work.
- The six Phase 1 ledger rows are reconciled to commit/runtime evidence.
- The targeted audit delta is written.
- Phases 2–4 remain explicitly incomplete.

Stop immediately after the targeted Phase 1 audit delta. Do not continue into
another audit loop.

## Execution result — 2026-07-12

- Checkpoint 1 complete: supply-chain evidence committed as `b71e18f`.
- Checkpoint 2 complete: Qdrant transport/TLS package committed as `9b3e16c`;
  final verification corrections are `8d80abb` and `132e5d0`.
- Checkpoint 3 complete under the V3 single-run rule. The one full non-ML run
  reported 3,940 passed and 10 failures; those exact failures then passed, and
  the focused R1.4 integrity gate passed 46 with 1 environment skip. The full
  suite was intentionally not rerun. Collection was 4,126; required Qdrant ML
  was 38 passed; Ruff, Compose contracts, clean-wheel install, and SBOM binding
  passed. Dependency audits timed out and approved local product images were
  unavailable, so those checks are `BLOCKED-EXTERNAL`.
- Checkpoint 4 complete: targeted delta is
  `.planning/audits/2026-07-12-phase1-budget-delta/audit-delta.md`.
- Phases 2-4 remain incomplete and were not executed.

## Replacement goal prompt

```text
/goal Finish MemoryMaster Phase 1 in budget mode using the isolated worktree
`G:\tmp\memorymaster-remediation-20260710` and
`.planning/REMEDIATION-EXECUTION-V3-BUDGET.md` as the controlling scheduler.
Keep `.planning/REMEDIATION-OPTIMIZATION-PLAN-2026-07-10.md` and
`.planning/audit-remediation-ledger.md` as audit sources, but let V3 override V2
for test frequency, review loops, audit scope, and completion criteria.

Preserve the main checkout and all existing user changes. Resume the current
uncommitted five-file supply-chain package and the frozen Qdrant worker package
at `G:\tmp\memorymaster-remediation-agents\r15-qdrant`. Freeze scope: fix only
reproducible Critical/High security or integrity defects in changed Phase 1
code; backlog Medium/Low and out-of-scope observations. Do not add features,
broad refactors, or release automation.

Use at most one child beside root. Perform one time-boxed Sol/high read-only
security review at the Qdrant/combined integration boundary and allow at most
one correction batch. Do not rerun the full suite after individual edits. Run
syntax/import checks while editing, one focused package gate before each atomic
commit, then the full non-ML suite, required Qdrant ML tests, Ruff, collection,
Compose contracts, clean-wheel/SBOM validation, and locally available scanners
once at the final integration boundary.

Keep all existing safety constraints: temporary/fake services by default;
GitNexus impact before editing existing symbols and change detection before
commits; embedding-preserving reindex; no push, publish, deploy, credential
rotation, history rewrite, live DB mutation, cleanup, redaction, migration, or
backlog operation. Record unavailable infrastructure and the unsuppressed
Gitleaks history findings as BLOCKED-EXTERNAL without suppressing or claiming a
pass.

Reconcile only MM-SEC-01, MM-SEC-02 Phase 1 containment, MM-SEC-03, MM-SEC-04,
MM-OPS-01, and MM-OPS-04 Phase 1 defaults. Produce one targeted Phase 1 audit
delta. Completion requires no unresolved reproducible Critical/High regression
introduced by the Phase 1 branch; Medium/Low items may remain documented
backlog. Stop after the delta. Do not run a full 13-domain audit or begin Phases
2–4.
```
