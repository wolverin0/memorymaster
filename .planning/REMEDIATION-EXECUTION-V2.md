# MemoryMaster Remediation Execution V2

**Status:** execution overlay
**Audit source of truth:** `.planning/REMEDIATION-OPTIMIZATION-PLAN-2026-07-10.md`
**Finding source of truth:** `.planning/audit-remediation-ledger.md`

This file changes scheduling and model routing, not audit facts. The original
roadmap remains immutable evidence. V2 minimizes wall-clock time, premium-model
usage, and main-thread context pollution while preserving every security gate.

## Recommended scope

Finish **Phase 1 security convergence**, produce a same-scope audit delta, and
then stop. Keep Phases 2-4 as a prioritized backlog unless the operator starts a
separate optimization goal. Production-grade multi-user ambitions can justify
that later program; they are not prerequisites for a hardened local deployment.

## Model-routing policy

| Work | Agent | Model / effort | Write authority |
|---|---|---|---|
| Code mapping, inventories, impact evidence | `mm_explorer` | Terra / low | None |
| Focused tests, Ruff, scanners, evidence | `mm_test_runner` | Terra / low | Runtime artifacts only |
| Ledger, audit delta, runbooks | `mm_docs_ledger` | Terra / medium | Explicit docs only |
| LOW/MEDIUM isolated implementation | `mm_fast_worker` | Terra / medium | Explicit owned files |
| Bounded multi-step implementation | `mm_worker` | GPT-5.6 Sol / medium | Explicit owned files |
| Security/integrity review | `mm_security_reviewer` | GPT-5.6 Sol / high | None |
| Architecture, HIGH/CRITICAL decisions, merge, final audit | root | Highest selected level | Integration worktree |

Rules:

- Root plus at most three workers run concurrently (`max_threads = 4`).
- Use the smallest context fork that contains the task; every worker prompt must
  be self-contained and name owned files, acceptance evidence, and stop rules.
- Terra workers must stop and reroute any newly discovered HIGH/CRITICAL edit.
- Only one write agent owns a worktree. Read-only workers may inspect it.
- Subagents run focused gates. Root runs cross-package regression and the full
  verification ladder at integration boundaries.
- Parallelism saves wall-clock and premium-model usage, not necessarily total
  tokens. Do not fan out work that shares files or requires one architectural
  decision.

## Worktree and merge protocol

1. Root owns `remediation/audit-convergence-20260710` in the existing isolated
   checkout and first commits or cleanly shelves the current R1.4 auxiliary work.
2. Each write package starts from that integration commit in a dedicated branch
   and worktree under a temporary remediation-agent root.
3. The parent prompt declares `owns=<paths>`. Workers never edit outside it.
4. A worker returns its diff, targeted evidence, and risks. It does not merge.
5. Root reviews, runs GitNexus change detection, integrates one package at a
   time, executes the package regression gate, commits atomically, and refreshes
   the embedding-preserving GitNexus index.
6. After a wave, root runs the full non-ML suite once, required ML gates once,
   Ruff, runtime smoke checks, and ledger reconciliation.

## Phase 1 dependency waves

### Wave A0 - preserve current work (serial root)

- Finish independent review of the already-green auxiliary persistence package.
- Run its focused/ML gates, commit it atomically, and refresh GitNexus.
- No other write worker touches the integration checkout during A0.

### Wave A1 - finish R1.4 (parallel after A0)

| Lane | Owner | Scope |
|---|---|---|
| Atlas envelope | `mm_worker` | Atlas source/evidence/provenance writes, legacy reads, ADR reconciliation |
| Remaining writers | `mm_worker` | Steward, compact summaries, miners, imports, bridges, merge/delta paths |
| Legacy inventory | `mm_explorer` + `mm_docs_ledger` | Dry-run inventory across primary DB, verbatim, Qdrant payloads, artifacts; no cleanup |

Root integrates lanes sequentially, resolves overlap at the canonical gateway,
and runs the complete R1.4 table-driven adversarial matrix. Live cleanup remains
forbidden and is recorded as an external action.

### Wave B - R1.5 secure deployment defaults (parallel after R1.4)

| Lane | Owner | Scope |
|---|---|---|
| Deployment contracts | `mm_fast_worker` | Compose/Helm secrets, private bindings, immutable image references |
| Qdrant transport | `mm_worker` | API key/TLS propagation and fake-service tests |
| Supply-chain evidence | `mm_fast_worker` + `mm_test_runner` | Secret/history scan, dependency/image scan wiring, SBOM artifact |

Anything requiring real credential rotation, authenticated infrastructure, image
registry access, or deployment becomes `BLOCKED-EXTERNAL` with exact operator
commands in `external-actions-required.md`.

### Wave C - Phase 1 convergence (serial root plus read-only reviewers)

1. Run the Phase 1 verification ladder from the original roadmap.
2. Rerun hard-stops, blind spots, and affected audit domains against the exact
   baseline scope.
3. Produce an audit delta and reconcile every Phase 1 ledger row to commit and
   runtime evidence.
4. Use Terra agents for file-by-file evidence gathering and GPT-5.6 Sol/high for the
   independent security review.
5. Fix newly introduced Phase 1 findings until the latest same-scope audit has
   zero new findings and every Phase 1 item is resolved or validly blocked.
6. Stop. Do not begin R2-R4 under this goal.

## Goal completion contract

Completion means Phase 1—not the entire optimization roadmap—is proven:

- R1.1-R1.5 repository work is resolved or validly blocked.
- Disabled profiles remain fail-closed where external parity is unavailable.
- The full required verification ladder is green or has explicit external
  blockers without false success claims.
- The Phase 1 audit delta has zero new findings in its latest run.
- Phases 2-4 are preserved as prioritized backlog with no implied completion.

## Replacement goal prompt

```text
/goal Execute Phase 1 security convergence for MemoryMaster using
`.planning/REMEDIATION-EXECUTION-V2.md` as the scheduler and
`.planning/REMEDIATION-OPTIMIZATION-PLAN-2026-07-10.md` plus
`.planning/audit-remediation-ledger.md` as the audit sources of truth.

Resume from the isolated remediation branch/worktree and preserve the current
uncommitted R1.4 auxiliary package. First finish its review, verification,
atomic commit, and embedding-preserving GitNexus refresh. Then execute Waves A1,
B, and C in dependency order.

Use project custom agents and explicit model routing: Terra/low for read-only
mapping and test evidence, Terra/medium for LOW/MEDIUM isolated work and docs,
GPT-5.6 Sol/medium for bounded multi-step implementation, GPT-5.6 Sol/high for
independent security review, and the root/highest level only for architecture,
HIGH/CRITICAL decisions, integration, and final convergence. Run at most three
children beside root. Give each write worker its own worktree and explicit owned
files; never allow concurrent writers in one checkout. Reroute newly discovered
HIGH/CRITICAL edits from Terra to root and warn before proceeding.

Keep all existing safety constraints: query MemoryMaster before architecture
decisions when available; run GitNexus impact before symbol edits and change
detection before commits; add witnessed adversarial RED tests before security or
integrity fixes; preserve user changes; use temporary databases/fake services;
never mutate the live MemoryMaster DB, push, publish, deploy, rotate credentials,
or perform external product-data changes without explicit approval. Record real
external blockers in `external-actions-required.md` and continue unblocked work.

Workers run focused verification; root runs cross-package gates and the full
Phase 1 verification ladder at integration boundaries. Rerun the same-scope
Phase 1 audit, reconcile the ledger to commit/runtime evidence, and fix new
Phase 1 findings until every item is resolved or validly blocked and the latest
audit has zero new findings. Stop after the Phase 1 audit delta. Do not execute
Phases 2-4; preserve them as prioritized backlog for a separate goal.
```
