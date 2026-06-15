# Integrating an agent with MemoryMaster — the 3-Beat Contract

> Scope: how ANY AI coding agent (Claude Code, Codex, Gemini, droid, opencode, or a
> remote VM bridge like Hermes) uses MemoryMaster for cross-session memory.
> Companion design map: `.planning/P4-AGENTS-CONTRACT.md` (file:line evidence).

Every agent that uses MemoryMaster runs the **same three beats**. The beats are the
contract; the *mechanism* differs per agent class (installed hooks vs. a reference
script vs. an external bridge), but the **rules underneath are identical** because all
writes go through one hardened ingest path.

---

## 0. The three beats

| Beat | Name | What it does | When |
|------|------|--------------|------|
| **1** | session-start **FETCH** | Inject recent claims, last-cycle summary, pending candidates, and recently-updated wiki for the current scope so the agent starts *warm*. | once, at session start |
| **2** | on-demand **RECALL** | Per-prompt search of the DB, injected as invisible context. Read-only. | every prompt |
| **3** | session-end **DISTILLED INGEST** | Distill **≤3** learnings, set `source_agent`, route through the documented ingest path. **Never** a raw `INSERT`. | once, at session end (or checkpoint) |

BEAT 3 is the only beat that *writes*. It is therefore the beat governed by the
shared writer discipline below, and the beat most often missing for non-Claude agents.

---

## 1. The shared writer discipline (sits UNDER all three beats)

This is the part that does NOT change per agent. Every write — from a Claude hook, the
Codex reference script, the CLI, MCP, or the Hermes bridge — converges on
`MemoryService.ingest` (`memorymaster/core/service.py`), where two gates run in order:

1. **Sensitivity filter** (`security.sanitize_claim_input`, `service.py:464`) — the
   firewall. Redacts/blocks secrets (API keys, tokens, private IPs, credentials, card
   data, username-leaking paths, raw code). This is **sacred**: there is no
   `allow_sensitive=True` bypass on ingest, and you must not add one. See
   `.claude/rules/sensitivity-filter.md`.
2. **Intake policy** (`intake_policy.evaluate_intake`, `service.py:483`, P3) — additive
   admission control. It may reject *more* or default-tag attribution; it never flips a
   prior reject into an accept and never weakens the filter. It is what makes
   `source_agent` reliable (Rule A) and what fences batch floods (Rule D).

### WAL-Discipline, not a write-broker (P1)

There is **no central write-broker process**. Instead, every agent writes through the
same *hardened connection envelope* (`MEMORYMASTER_WAL_DISCIPLINE`): WAL mode, bounded
busy-timeout, and (optionally) a spool regime where per-stop hooks append cheap
envelopes that the steward drain later replays through `service.ingest`. The discipline
is enforced by the connection layer + the ingest chokepoint, not by a gatekeeper
daemon. Practical consequence: **any** code path that wants to write claims must call
`service.ingest` (or CLI `ingest` / MCP `ingest_claim`, which both delegate to it) — a
raw `INSERT INTO claims` bypasses *both* the sensitivity filter and the intake policy
and is a defect.

### The three non-negotiable BEAT-3 rules

1. **`source_agent` is always set, never NULL.** It is the attribution key for the
   provenance view (§4). Omitting it lands the claim as `unknown` (Rule A warn default)
   or `mcp-session` (MCP default) — provenance noise.
2. **≤3 distilled learnings per session end.** Distill, do not capture verbatim
   per-turn. Enforce with both a `[:3]` slice *and* a per-session `intake_batch_id` +
   `intake_batch_max=3` so the intake policy (Rule D) fences a flood even if the slice
   is tampered with.
3. **No session-state claims.** Ingest decisions, bug root causes, gotchas,
   constraints, integration patterns — not "currently working on X". Convert relative
   dates ("Thursday") to absolute ISO-8601 before storing.

---

## 2. Per-agent-class how-to

### Claude Code — fully turnkey (installed hooks)

`setup_hooks.py` installs three hooks; no manual wiring needed.

| Beat | Mechanism | Template |
|------|-----------|----------|
| 1 FETCH | `SessionStart` hook injects recent claims + cycle summary + candidates + wiki via `additionalContext` | `config_templates/hooks/memorymaster-session-start.py` |
| 2 RECALL | `UserPromptSubmit` hook runs `recall()` read-only and injects `[MemoryMaster recall]` | `config_templates/hooks/memorymaster-recall.py` |
| 3 INGEST | `Stop` hook: block-to-save every 15 msgs + passive ≤3 distill → `service.ingest` with `source_agent="llm-stop-hook"`; the block reason tells Claude to ingest with `source_agent="claude-session"` | `config_templates/hooks/memorymaster-auto-ingest.py` |

Nothing to do beyond running `python scripts/setup-hooks.py`.

### Codex / generic MCP agents — AGENTS.md (instruction) + reference script (automation)

There are **two layers**, and you need both:

**Instruction layer** — `setup_hooks.append_instructions()` appends
`config_templates/codex-agents-md-append.md` to `~/.codex/AGENTS.md` (CODEX_DIR-aware).
It tells the agent to `query_memory` before decisions (BEAT 1/2) and to `ingest_claim`
with `"source_agent": "codex-session"` (BEAT 3). This depends on the agent *choosing* to
comply.

**Automation layer** — because Codex has **no native `Stop` hook**, BEAT 3 will silently
not fire if the agent forgets. The turnkey closer is:

```
python scripts/agent_session_end_ingest.py \
  --db <path>/memorymaster.db \
  --transcript ~/.codex/sessions/rollout-<id>.jsonl \
  --source-agent codex-session \
  --cwd <project-dir>
```

`scripts/agent_session_end_ingest.py` mirrors the Claude `Stop` hook discipline exactly:

- reads the tail of the transcript (handles both Claude-style `message` envelopes and
  Codex rollout `payload` envelopes),
- distills **≤3** learnings via the shared cheap LLM (`core.llm_provider`),
- drops anything that trips the sensitivity filter **before** ingest
  (`_is_sensitive_claim`, fail-closed),
- routes through `MemoryService.ingest` — **never** a raw `INSERT`,
- sets `source_agent` (default `codex-session`, required non-empty),
- stamps one `intake_batch_id` + `intake_batch_max=3` so Rule D fences the batch.

Wire it as a Codex notify/exit hook, run it manually at session end, or schedule it.
`setup_hooks` prints the exact command after appending AGENTS.md.

> **Why not the old autologger?** `scripts/scheduled_ingest.py` captures *every* turn
> verbatim (`claim_type=<connector>_turn`, conf 0.5) and historically did **not** pass
> `source_agent`, so its claims landed as `unknown` and flooded provenance. The
> reference script is the BEAT-3-correct replacement: distilled, attributed, fenced.

> **Generic agents** (Gemini, droid, opencode): same as Codex. If the agent has the 21
> MCP tools, BEAT 1/2 are manual `query_memory`/`query_for_context` calls and BEAT 3 is
> either a manual `ingest_claim` (MCP default-tags `mcp-session` if you omit
> `source_agent` — set it explicitly) or the reference script with `--source-agent
> <agent>-session`.

### Hermes — external VM bridge (contract only; not locally verifiable)

The Hermes bridge is built and operated **off this machine**. We cannot verify the VM
from here, so this section is the **contract its bridge MUST satisfy** on the local
side. Be honest about the boundary: what is locally verifiable is the *merge* path and
the ingest chokepoint; what is **not** verifiable here is whether the VM actually
routes through them.

| Requirement | Why | Local anchor |
|-------------|-----|--------------|
| Write claims ONLY via MCP `ingest_claim` or CLI `ingest` (→ `service.ingest`) | so the sensitivity filter + intake policy both run; a raw `INSERT INTO claims` bypasses both | `service.py:464` / `:483` |
| Pass an explicit `source_agent` (e.g. `"hermes-vm"`), never empty | so the provenance view (§4) can isolate Hermes activity | `claims.source_agent` |
| Let `db_merge` carry attribution verbatim on bidirectional sync | so Hermes-origin `source_agent` survives the merge and the merge isn't re-rejected by the chokepoint | `bridges/db_merge.py` (verbatim row copy incl. `source_agent`) |
| Hermes transcript content MUST pass the sensitivity filter on ingest | it is ambient transcript data; do not weaken the filter for the bridge | sensitivity-filter rule |

**Locally verifiable:** the merge preserves `source_agent`; the ingest chokepoint
enforces filter + policy. **NOT verifiable here:** that the VM routes through MCP/CLI and
sets `source_agent` — that is the operator's responsibility on the VM side.

---

## 3. The CLI/MCP/service paths that carry attribution

| Path | How to set `source_agent` |
|------|---------------------------|
| `MemoryService.ingest(...)` | `source_agent="codex-session"` kwarg (the canonical path) |
| MCP `ingest_claim` | `source_agent` field; **defaults to `mcp-session`** if omitted — set it |
| CLI `python -m memorymaster ingest` | `--source-agent codex-session` (added in P4; previously unattributed) |
| Reference script | `--source-agent` (required non-empty) |

All four converge on `service.ingest`. None of them should be replaced by direct SQL.

---

## 4. Per-agent provenance (dashboard panel)

Because P3 made `source_agent` reliable and the schema has `idx_claims_source_agent`, a
`GROUP BY source_agent` is cheap. The dashboard exposes it at **`GET /api/provenance`**
and renders a **"Provenance by Agent"** panel: one row per `source_agent` with total,
status mix (confirmed/candidate/stale/conflicted), 24h ingest count, and last-ingest
time.

**Honesty boundary baked into the panel:** *ingest* is attributed per agent
(`claims.source_agent`); *recall* is **not** — the `events` table has no `source_agent`
column, so a per-agent recall split would be fabricated. The endpoint returns
`attribution: {ingest_attributed: true, recall_attributed: false}` and the panel says so
rather than inventing recall numbers.

The query (read-only; `CASE`-based SUMs so it is valid on both SQLite and Postgres — no
schema change):

```sql
SELECT
  COALESCE(NULLIF(TRIM(source_agent), ''), '<null>') AS agent,
  COUNT(*)                                            AS total,
  SUM(CASE WHEN status='confirmed'  THEN 1 ELSE 0 END) AS confirmed,
  SUM(CASE WHEN status='candidate'  THEN 1 ELSE 0 END) AS candidate,
  SUM(CASE WHEN status='stale'      THEN 1 ELSE 0 END) AS stale,
  SUM(CASE WHEN status='conflicted' THEN 1 ELSE 0 END) AS conflicted,
  MAX(created_at)                                    AS last_ingest,
  SUM(CASE WHEN created_at >= :cutoff_24h THEN 1 ELSE 0 END) AS ingests_24h
FROM claims
WHERE status != 'archived'
GROUP BY agent
ORDER BY total DESC;
```

This panel directly visualizes the §2 gap: until the Codex BEAT-3 script runs, Codex
rows appear as `<null>`/`unknown`/`mcp-session` instead of a clean `codex-session` total.

---

## 5. Verifying the contract locally

A round-trip test (`tests/test_agent_contract_roundtrip.py`) asserts, for the
locally-testable paths, that a claim ingested via the documented path with a given
`source_agent` is (1) recallable via `query` and (2) counted under that agent in
`_provenance_rows`, plus that the reference script sets attribution, caps at 3, drops
sensitive learnings, and that the `/api/provenance` route serves the buckets.

Honesty boundary (matches `.planning/P4-AGENTS-CONTRACT.md §5`): the Claude hooks, the
AGENTS.md append, the intake policy wiring, the provenance panel, and the reference
script are **verified locally**. The **Hermes VM internals** and the **live
`memorymaster.db` contents** are **not** touched or verified from here.
