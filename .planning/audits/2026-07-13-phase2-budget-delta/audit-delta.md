# MemoryMaster Phase 2 Budget Audit Delta

**Date:** 2026-07-13

**Baseline:** `.planning/audits/2026-07-10-baseline/audit-report.md`

**Scheduler:** `.planning/REMAINING-PHASES-V1-BUDGET.md`

**Scope:** MM-SEC-02, MM-ARCH-01, MM-ARCH-02, MM-LIFE-01, MM-REL-02,
MM-PRIV-01, MM-COST-01, MM-COST-02, MM-COST-03, and MM-DEMO-01 only.

## Verdict

Phase 2 repository convergence is complete for its scheduled scope. No
unresolved reproducible Critical/High regression introduced by the Phase 2
branch remains. MM-COST-03 remains an explicit Medium R3.2 backlog item.
MM-SEC-02 and MM-PRIV-01 remain blocked on external runtime and legal/product
evidence. This was a targeted Phase 2 reconciliation, not a 13-domain reaudit.

## Finding delta

| Finding | Disposition | Phase 2 evidence |
|---|---|---|
| MM-SEC-02 | `BLOCKED-EXTERNAL` | P2-B exposes only ID/hash/score candidates, rehydrates from the authoritative store, rejects policy/hash/lifecycle failures, and provides exact reconciliation plus a bounded metadata-only outbox. Semantic reads stay default-off; authenticated/TLS Qdrant parity is unavailable. |
| MM-ARCH-01 | `RESOLVED` | P2-D establishes the integer-ID registry, immutable migration 0013, read-only readiness failures, backend schema parity, MCP integration, and zero-FK validation. |
| MM-ARCH-02 | `RESOLVED` | P2-A/P2-B unify retrieval. P2-Z witnessed a downstream prompt-stream bypass returning allowed, candidate, foreign, and private IDs together, then applied the same trusted planner policy before and after every optional stream. |
| MM-LIFE-01 | `RESOLVED` | P2-C routes scheduled archival through canonical optimistic transitions, events, timestamps, cache invalidation, and replayable vector deletion. |
| MM-REL-02 | `RESOLVED` | P2-C makes top-level recall query-only, removes duplicate detail retrieval, and emits one aggregate telemetry envelope. |
| MM-PRIV-01 | `BLOCKED-EXTERNAL` | Capture is quiet by default and maximum capture/providers require explicit configuration. Product-owner/legal decisions on use, processors, jurisdictions, and retention remain external. **WARNING LEGAL REVIEW REQUIRED; no compliance-pass or risk acceptance is claimed.** |
| MM-COST-01 | `RESOLVED` | P2-E persists atomic restart-safe global/provider/session reservations and usage; default candidate inflow is capped at 600/day. |
| MM-COST-02 | `RESOLVED` | P2-E persists complete-line cursors and finite 30-day/512-MiB/75,000-session dry-run retention bounds. |
| MM-COST-03 | `IN-PROGRESS` | Capture-hook accounting is durable, but core LLM, embedding, provider, and MCP quotas are not yet one account-wide ledger. Continue in R3.2. |
| MM-DEMO-01 | `RESOLVED` | P2-F requires a real production provider and prevents explicitly opted-in synthetic development evidence from reaching governed truth, prompts, actions, citations, or exports. |

## Implemented evidence

- `7026711` — immutable governed retrieval planner.
- `3e97e5b` — governed Qdrant reintegration and replayable outbox.
- `732cb68` — lifecycle authority and read-only recall.
- `4fb1725` — entity schema convergence through migration 0013.
- `a1d5477` — quiet finite capture, budgets, cursors, and retention.
- `c66e240` — fail-closed authentic media evidence defaults.
- The conventional P2-Z commit containing this delta — prompt-stream
  governance correction, compatibility convergence, and exact boundary proof.

## Verification boundary

| Gate | Exact result |
|---|---|
| Targeted Phase 2 matrix | 226 passed, 2 failed; exact two-failure correction: 3 passed. |
| Full non-ML, single completion | 3,964 passed, 28 failed, 10 errors, 70 skipped, 95 deselected, 3 xfailed, 1 warning in 915.59s. |
| Bounded correction matrix | 287 passed, 12 failed in 31.99s. |
| Remaining-cluster verification | 71 passed, 1 failed in 2.82s. |
| Final exact scope correction | 2 passed. |
| Interpretation | Compositional convergence evidence. The 15-minute full suite was not rerun and no second clean full-suite invocation is claimed. |
| Collection | 4,171 tests collected. |
| Required Qdrant ML | 34 passed. |
| Ruff | Project and changed-file Ruff clean after one exact unused-import correction. |
| Diff integrity | `git diff --check` clean. |
| Disposable services | No Postgres DSNs/safe opt-in and no Qdrant URL/API key/CA were configured. |

## External blockers

- Disposable authenticated/TLS Qdrant parity remains required before enabling
  governed semantic production reads.
- Disposable two-role Postgres parity remains required; no product database was
  contacted or mutated.
- The product owner and legal reviewer must approve the privacy, processor, and
  retention statement before compliance claims.

See `external-actions-required.md` for owners and evidence requirements.

## Backlog boundary

- MM-COST-03 continues in R3.2 for account-wide durable quota convergence.
- R3.1-R3.5 cover performance, setup truth, service readiness, recovery,
  observability, audit, and privacy operations.
- R4.1-R4.4 cover extensions, decomposition, governance UX, accessibility, and
  generated release truth.

## Rollback

- Disable semantic Qdrant reads first and fall back to authoritative lexical
  retrieval.
- Return capture to quiet/no-capture; never restore unlimited per-stop capture.
- Disable media enrichment instead of restoring synthetic production evidence.
- Revert package commits in reverse order only in a disposable/staged
  environment.
- Never edit or reverse migration 0013 in place; use a verified backup or a
  documented forward repair.
- Preserve metadata-only Qdrant outbox records until reconciliation is safely
  disabled or replayed.
