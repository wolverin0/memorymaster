# MemoryMaster — Roadmap

**Posture as of v3.18.0 (2026-05-17).** R@5 = 0.972 on LongMemEval-S leads the small
published set (MemPalace 0.966, agentmemory 0.952). The recall race is approaching
diminishing returns. The next two phases harden production-grade claims and then
unlock the governance/MCP differentiators.

The original v3.18.0 roadmap (governance-first) was incomplete: it assumed the
security/ops baseline was already in place. A second-opinion review by GPT-5.4
Thinking surfaced real hardening gaps that have to come first. This document
merges both lenses.

## Phase 0 / v3.19.0 — Hardening (highest urgency)

These items lock in the "production-grade" claim. Without them, the rest of the
roadmap is selling on a foundation that doesn't actually meet its name.

### v3.19.0-H1 — LLM budget caps per cycle  **[MOST URGENT]**

**Problem.** No `MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE` / `MAX_TOKENS_PER_CYCLE`
/ `MAX_PROVIDER_FAILURES_PER_CYCLE` env vars exist. Steward / wiki-absorb /
daydream-ingest can hit ~200 LLM calls per run. Provider stuck in retry-loop
burns quota silently. This session literally burned Claude Pro/Max quota on
the A1 bench with no per-cycle cap.

**Scope.** Add per-cycle counters in `service.run_cycle`, `wiki_engine`, and
`jobs/daydream_ingest.py`. Enforce hard stops with explicit reason codes.
Emit metrics. Add circuit breaker per provider.

**Tests.** Simulate quota exhaustion, repeated provider failure, budget exhaustion.
Assert run terminates visibly + reason logged + counters incremented.

### v3.19.0-H2 — Dashboard auth enforcement audit + fill gaps

**Problem.** `access_control.py` exists, but coverage is partial. GET routes
(claims, events, observability, SSE) and POST routes (proposals, operator
control) need consistent role enforcement: `viewer` (read) vs `operator`
(mutate). Loopback bind by default; refuse non-loopback without auth secret.
CSRF/origin checks for browser POSTs.

**Tests.** Anonymous / viewer / operator role matrix. SSE auth. CSRF rejection.
Non-loopback bind refusal without secret.

### v3.19.0-H3 — Webhook HMAC signing + replay window

**Problem.** Operator-emitted webhooks send claim data with no signature.
Receivers can't tell trusted from forged.

**Scope.** `X-MemoryMaster-Signature` (HMAC-SHA-256) + `X-MemoryMaster-Timestamp`
on all outbound webhooks. Configurable secret. Inbound verification helper.
Reject outside replay window.

**Tests.** Valid signature / invalid signature / altered body / replayed request.

### v3.19.0-H4 — MCP mutating-tool allowlist for DB/workspace overrides

**Problem.** MCP tools accept caller-controlled `db` and `workspace` paths,
including mutating tools. A caller can write to any SQLite file the process
can reach.

**Scope.** Classify tools read-only vs mutating. Remove per-call DB override
on mutating tools, or guard behind explicit admin config. Allowlist-validate
workspace roots. Persist actor/request id on mutations.

**Tests.** Allow/deny path resolution. Mutating tool with default DB only.
Allowlist matches/misses.

## Phase 1 / v3.20.0 — Storage discipline

### v3.20.0-S1 — Versioned migrations + SQLite/Postgres parity gate

**Problem.** Schema evolution today is opportunistic `ALTER TABLE` with
try/except — no version tracking, no rollback, parity drift risk between
SQLite and Postgres backends.

**Scope.** Schema version marker table. Forward-apply migration files.
Parity test suite for claims/citations/lifecycle/events/retrieval. CI gate.

### v3.20.0-S2 — Remove remaining `sqlite3.connect` bypasses (if any)

**Cross-check 2026-05-17.** Already 0 matches in `mcp_server.py`. Verify other
modules; if all clean, mark this S-item DONE in this commit and skip the PR.

## Phase 2 / v3.21.0+ — Architecture & differentiator features

### v3.21.0-A1 — Split the worst oversized modules

**Cross-check 2026-05-17.** 10 files >800 LOC. Top offenders:

  - `postgres_store.py` 2591 LOC
  - `context_hook.py` 2004 LOC
  - `steward.py` 1651 LOC
  - `dashboard.py` 1491 LOC
  - `operator.py` 1453 LOC
  - `mcp_server.py` 1439 LOC
  - `cli_handlers_basic.py` 1359 LOC
  - `service.py` 1357 LOC

Split by domain workflow, not "helpers.py dumping". Maintain public CLI/MCP
surfaces. Tests stay green.

### v3.21.0-A2 — Thin `MemoryService` facade into explicit pipelines

`IngestPipeline`, `RetrievalPlanner`, `LifecycleManager`, `StewardRunner`,
`WikiProjector`. `MemoryService` becomes a stable thin facade.

### v3.21.0-D1 — Conflict-resolution UI for the dashboard *(governance differentiator)*

`conflicted` claims today require MCP `pin`/`redact`/`supersede` calls.
A side-by-side conflict view that resolves in one click converts governance
from "exists in code" to "exists in workflow." This is the differentiator
nobody else has.

### v3.21.0-D2 — Steward observability

JSONL audit log + CLI viewer. "What did the steward promote, demote, archive
over the last 30 days." Makes the cycle's effect legible.

### v3.21.0-D3 — Wiki coverage report

Surface scopes with low article coverage relative to claim volume.

### v3.21.0-D4 — MCP self-documentation tool

A `mcp_help` tool that returns the decision tree for which tool to call when
(query_for_context vs query_for_task vs query_memory). Smooths agent onboarding.

### v3.21.0-D5 — A1 full 500q publication run

Mechanism shipped in v3.18.0 (#109). Run the overnight bench when budget allows
(now safer once v3.19.0-H1 budget caps are in). Publish first full QA-accuracy
number on LongMemEval-S; update README.

### v3.21.0-D6 — Silent-except cleanup *(downgraded — cross-check shows 0 `except: pass`)*

GPT-5.4 review claimed 35 silent excepts. Cross-check 2026-05-17: 0 actual
`except: pass` patterns. The 428 `except` clauses look like proper typed handling.
Defer — audit one module at a time during natural refactors. Don't dedicate a sprint.

## Where NOT to spend cycles

- **Chasing R@5 to 0.99.** Marginal benchmark gains don't translate to user-facing
  value once we're already at the top of the published set. Same effort spent
  on Phase 0 hardening yields more.
- **Re-architecting the retrieval blend.** Linear blend at current weights is
  at or near its local optimum (RRF tiebreaker NULL, session-diversity NULL,
  Gemini LLM rerank NULL, W_LEX sweep REVERT — all v3.16).
- **Adding a fifth LLM provider** unless an integration explicitly requires it.
  The 4 + `claude_cli` OAuth already cover the common case.
- **Marketing the differentiators (Phase 2) before Phase 0 lands.** Auth-less
  dashboards and unbounded LLM budgets undermine any "production-grade" claim.

## Order of work

1. v3.19.0-H1 (LLM budget caps) ← most urgent; do first
2. v3.19.0-H2 (dashboard auth)
3. v3.19.0-H3 (webhook HMAC)
4. v3.19.0-H4 (MCP mutating allowlist)
5. → ship v3.19.0
6. v3.20.0-S1 (migrations + parity gate)
7. → ship v3.20.0
8. v3.21.0-A1 + A2 (module splits, thin MemoryService)
9. v3.21.0-D1 (conflict-resolution UI — first differentiator user can see)
10. v3.21.0-D5 (A1 publication run, now safe with budget caps)
11. v3.21.0-D2/D3/D4 in any order

Each numbered item is a separate PR. None requires benchmark grinding.
