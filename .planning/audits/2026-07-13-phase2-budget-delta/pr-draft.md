# Draft PR — Governed MemoryMaster Phase 2 Core

## Suggested title

`feat: converge governed MemoryMaster Phase 2 core`

## Summary

- Unifies trusted retrieval across service, MCP, prompt hooks, and gated
  Qdrant candidate discovery.
- Rehydrates semantic candidates from the authoritative store and reconciles
  exact IDs/content hashes through a bounded replayable outbox.
- Routes archival through canonical lifecycle authority and makes top-level
  recall query-only with aggregate telemetry.
- Converges entity/graph schemas through immutable migration 0013 and explicit
  readiness checks.
- Makes capture quiet, finite, replay-safe, budgeted, and retention-bounded.
- Prevents synthetic media from becoming production evidence.
- Corrects a convergence-discovered prompt/entity/graph policy bypass.

## Verification

- Targeted Phase 2 matrix: 226 passed; its two failures passed in the exact
  3-test correction slice.
- Single full non-ML invocation: 3,964 passed, 28 failed, 10 errors, 70 skipped,
  95 deselected, and 3 xfailed in 915.59s.
- Bounded reconciliation: 287 passed/12 failed, then 71 passed/1 failed, then
  the final two affected tests passed.
- Collection: 4,171 tests.
- Required Qdrant ML: 34 passed.
- Project/changed Ruff and `git diff --check`: clean.

The full-suite failures were reconciled compositionally under the budget
scheduler. This PR does not claim a second clean full-suite invocation.

## External blockers

- Real authenticated/TLS Qdrant parity.
- Disposable two-role Postgres evidence.
- Approved privacy/data-processing and retention statement.
- Semantic/team production profiles remain disabled until their required
  evidence exists.

## Rollback

Disable semantic reads, capture, and media enrichment first. Revert package
commits in reverse order only in a disposable/staged environment. Do not edit
immutable migration 0013; restore a verified backup or use a forward repair.
No production rollback command is required because this branch is not deployed.
