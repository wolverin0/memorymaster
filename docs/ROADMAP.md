# MemoryMaster — Roadmap

**Posture as of v3.18.0 (2026-05-17).** R@5 = 0.972 on LongMemEval-S currently leads
the small published set (MemPalace 0.966, agentmemory 0.952). Pushing R@5 from 0.97
toward 1.0 saves ~10 questions out of 500 — diminishing returns on the recall axis.

The competitive advantage is NOT in another decimal of R@5. It is in the things
none of the competitors do: lifecycle governance, multi-provider abstraction, an
ingest pipeline with security baked in, and an MCP-first interface that drops into
any LLM agent today. The work below reflects that priority order.

## Themes

### 1. Lifecycle & governance differentiator (highest leverage)

What MemoryMaster does that nobody else does well: claims have a **status** (`candidate
→ confirmed / stale / conflicted / superseded / archived`), a **tier**, **bitemporal
fields**, and a **steward cycle** that validates, decays, and promotes them. The wiki
("compiled truth + timeline") is downstream of this.

Gaps worth closing:

- **Conflict resolution UI.** `conflicted` claims today require manual `pin` /
  `redact` / `supersede` actions through MCP. A dashboard view that surfaces conflict
  pairs side-by-side and lets the operator resolve in one click would convert the
  governance promise from "exists in code" to "exists in workflow."
- **Steward observability.** `run_cycle` returns a result dict but there is no
  long-running view of "what did the steward do over the last 30 days, what did it
  promote, demote, archive?" A simple JSONL audit log + a CLI viewer would make the
  cycle's effect legible.
- **Wiki coverage report.** `wiki-absorb` produces articles; we have no view of
  "which scopes have low article coverage relative to claim volume". Surfacing
  coverage gaps drives the next absorb / breakdown cycle.

### 2. The QA-accuracy publication (A1 follow-through)

The claude_cli judge provider shipped in v3.18.0 unblocks the run. The mechanism is
proven (smoke-10 hit 0.50 accuracy, 120/500 of an overnight run was stable at the
same range before user-pause). Remaining work is operational, not technical:

- **Run the full 500q overnight pass** when there is the wall-clock budget and the
  user explicitly authorizes the Claude Pro/Max consumption.
- **Publish the number** in `docs/longmemeval-results.md` alongside the existing
  retrieval numbers. This becomes the first published full-QA-accuracy figure for
  any of the memory systems in this leaderboard.
- **Add a `--judge claude_cli` mention to README** so external users can reproduce
  without API keys.

### 3. Per-question-type profile tuning (S3 follow-through)

S3 E01 lifted the single-session-preference bucket from 0.80 → 0.90. The other
weak-ish buckets:

- **temporal-reasoning** (0.9549). E02 fresh-heavy NULLed because the bench's
  freshness anchor is degenerate. To explore further, either (a) modify the bench
  to use `item['session_date']` as the freshness anchor and re-test fresh-heavy,
  or (b) try vec-heavy and accept the bucket is already near its lex+vec ceiling.
- **single-session-user** (1.0000) and **knowledge-update** (0.9872): at or near
  ceiling, not worth chasing.
- **multi-session** (0.9774): unexplored. Hypothesis: a confidence-weighted
  profile may help when the answer is reinforced across multiple sessions.

Expected ceiling for overall R@5 with full per-type tuning: ~0.985. Marginal upside
≤ +0.013. Decide whether this is worth a session against the differentiator work.

### 4. The MCP-first interface (the moat that keeps growing)

Most competitors are libraries. MemoryMaster is a 21-tool MCP server that drops into
any Claude Code / Cursor / Codex session today. The investment areas:

- **Tool documentation surface.** `query_for_context` and `query_for_task` have
  different shapes; new users guess which to call. A `mcp_help` tool that returns
  the decision tree as a string would smooth onboarding.
- **Tool-call telemetry.** No view of "which agents call which tools how often."
  Useful both for popularity-driven tool prioritization and for catching agent
  misuse patterns.
- **A `replay_session` tool** that takes a transcript path and returns the claims
  that would be ingested (without ingesting). Lets agents preview ingest decisions
  before committing — relevant for the steward / dream-bridge pipelines.

### 5. Operational hardening

- **Sensitivity filter golden-set tests.** Filter has unit tests but no curated
  corpus of "things that MUST NOT pass" (real-looking API keys, JWTs, private IPs
  in various formats). Worth a `tests/fixtures/sensitivity_corpus.txt` + matching
  test that asserts each line is caught.
- **Postgres parity verification harness.** `db_merge.py` and `postgres_store.py`
  exist; no test that proves SQLite-write → Postgres-merge produces the same
  observable behavior end-to-end. One script that ingests N claims to both, queries
  both, asserts identical results.
- **Setup-hooks idempotency.** `scripts/setup-hooks.py` is run twice by some users
  on machine setup. Should be safe; should be tested.

## Where NOT to spend cycles

- **Chasing R@5 to 0.99.** Marginal benchmark gains do not translate to user-facing
  value once we're already at the top of the published set. The same effort spent
  on governance or MCP yields more.
- **Re-architecting the retrieval blend.** Linear blend at the current weights is
  at or near its local optimum (RRF tiebreaker NULL, session-diversity cap NULL,
  Gemini LLM rerank NULL, W_LEX sweep REVERT — all in v3.16). Don't re-explore.
- **Adding a fifth LLM provider** unless an integration explicitly requires it.
  The 4 + claude_cli OAuth already cover the common case.

## Suggested next-session topics

In rough priority order:

1. Conflict resolution UI for the dashboard (governance differentiator)
2. Steward observability — JSONL audit log + CLI viewer
3. A1 full 500q publication run + README mention
4. Sensitivity filter golden-set tests
5. MCP `mcp_help` tool + tool-call telemetry

Pick one based on the session's wall-clock budget. None of them require benchmark
grinding.
