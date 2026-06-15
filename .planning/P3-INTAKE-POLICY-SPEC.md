# P3 Intake Policy — Design Spec

Status: DESIGN (read-only phase, no code changes)
Branch: `omni/p3-quality`
Author: P3 quality subagent
Date: 2026-06-15

## 0. Non-negotiable safety boundaries (carried from the task)

1. The **sensitivity filter is sacred** (`memorymaster/core/security.py:sanitize_claim_input`,
   called from `service.ingest` line 432). The intake policy runs **alongside** it (both
   reject), never replaces or reorders it. Policy is evaluated as a **separate gate**; the
   sensitivity sanitize stays exactly where it is in the pipeline.
2. The policy is **purely additive**: it may REJECT more claims (raise the bar) but must
   NEVER cause a claim that was previously rejected to now be accepted. No new bypass flags.
3. Every rule is **configurable via env var with a SAFE DEFAULT** and **testable in isolation**.
4. Do NOT touch the live `memorymaster.db`. Tests use tmp DBs.
5. The working tree IS production for background steward/hooks — keep `service.ingest`
   importable and the ingest path working at all times.

---

## 1. Ingest chokepoint map (file:line)

The claims table is written through **two classes** of path: those that funnel through
`MemoryService.ingest` (the canonical chokepoint) and those that issue **raw `INSERT INTO
claims`** SQL and therefore BYPASS the filter + any future policy.

### 1a. Canonical chokepoint — `MemoryService.ingest`
- **`memorymaster/core/service.py:384`** — `def ingest(...)`. The single narrowest gate.
  Calls `sanitize_claim_input` (line 432) then `store.create_claim` (line 488).
  `source_agent` is an OPTIONAL kwarg defaulting to `None` (line 400) — this is the root
  cause of attribution loss (see §2).

Callers that correctly pass through it:
- `memorymaster/surfaces/mcp_server.py:558` — MCP `ingest_claim` (sets `source_agent` →
  `_empty_to_none(request.source_agent) or "mcp-session"`, line 555). Already has a
  per-source rate limiter `_check_ingest_rate_limit` (line 228, called 540 + 1175) and a
  sensitivity pre-check `_sensitive_input_error` (line 544).
- `memorymaster/surfaces/mcp_server.py:1189` — second MCP ingest entrypoint (rule/other).
- `memorymaster/govern/jobs/spool_drain.py:88` — `_replay_ingest` replays spool envelopes
  through `svc.ingest`. `_INGEST_FIELDS` (line 46) **includes `source_agent`** (line 57),
  so attribution is preserved on replay; `op:"dream"` defaults it to `"dream-bridge"` (line 77).
- `memorymaster/govern/jobs/daydream_ingest.py:106` — sets `source_agent="daydream"`.
- `memorymaster/recall/context_hook.py:2126` (`observe`, `source_agent="context-hook"`) and
  `:2170` (`observe_llm`, `source_agent="context-hook-llm"`).
- `memorymaster/knowledge/rule_miner.py:479,597` and `memorymaster/knowledge/rules.py:48` —
  set source_agent.
- `memorymaster/bridges/qmd_bridge.py:118` — `service.ingest(**params)`; source_agent only if
  `qmd_to_claims` populated it.
- `memorymaster/bridges/atlas_claim_extractor.py:63` — **NO source_agent** (NULL producer, low vol).
- `memorymaster/surfaces/cli_handlers_basic.py:340`, `cli_handlers_curation.py:608`,
  `operator.py:360` — **NO source_agent** (NULL producers, low vol).
- `memorymaster/config_templates/hooks/memorymaster-auto-ingest.py:208` (else branch) — sets
  `source_agent="llm-stop-hook"`; the spool branch (line 168 `spool.append`) also carries
  `source_agent="llm-stop-hook"` in the payload (line 178), drained via spool_drain → svc.ingest.

### 1b. Raw `INSERT INTO claims` — BYPASS the chokepoint (and the filter)
These do NOT pass through `service.ingest` and so will NOT see the policy unless policy is
ALSO mirrored at `store.create_claim` (see §3 placement note):
- **`memorymaster/bridges/dream_bridge.py:713`** — `dream_ingest` raw insert, **NO source_agent**,
  **bypasses sensitivity filter**. (Pre-existing finding; out of scope to fix here but flagged.)
- **`memorymaster/knowledge/transcript_miner.py:136`** — raw insert, hardcodes
  `source_agent='transcript-miner'` (NOT null), bypasses filter.
- **`memorymaster/govern/llm_steward.py:803`** — steward extra-extraction insert, **NO
  source_agent**, status `'confirmed'`. Runs every cycle → meaningful NULL contributor.
- **`memorymaster/bridges/db_merge.py:257`** — OpenClaw bidirectional merge. Copies the row
  **verbatim including the original `source_agent`** via dynamic column list → re-ingest is
  attribution-preserving. MUST be exempt from policy (see §4 risk).
- **`memorymaster/bridges/delta_sync.py:129`** — delta export to a NEW out-DB (not the live
  claims table); copies all columns verbatim. Not a claims-DB ingest; out of policy scope.
- `memorymaster/stores/_storage_write_claims.py:82` — `create_claim` SQLite (the real writer).
- `memorymaster/stores/postgres_store.py:415` — `create_claim` Postgres (parity writer).
- `memorymaster/stores/_storage_schema.py:368+` — `claims_fts` virtual-table inserts (index
  maintenance, not claim rows).

**Conclusion on placement:** `service.ingest` is the narrowest *intended* chokepoint and
where the policy SHOULD live (it already owns sensitivity + dedup + observability). But it is
NOT the *only* writer. `store.create_claim` (`_storage_write_claims.py:82` / `postgres_store.py:415`)
is the true last common writer. **Recommendation: policy primary in `service.ingest`; a thin
"defense-in-depth" assertion at `create_claim` is OPTIONAL/phase-2** — and if added must
exempt `db_merge` replay (see §4). For P3 the watchkeeper flood and 80% of NULLs enter via
**MCP `ingest_claim` → service.ingest**, which the chokepoint policy fully covers.

---

## 2. The 62%-NULL-source_agent finding — diagnosed against live data

Measured on the live DB (read-only, `mode=ro`), total **86,285** claims:

| source_agent | count | % |
|---|---|---|
| **`<NULL>`** | 40,802 | **47.3%** |
| llm-stop-hook | 34,281 | 39.7% |
| claude-session | 7,309 | 8.5% |
| codex-session | 3,297 | 3.8% |
| (long tail of named agents) | ~600 | <1% |

(The "62%" baseline figure is directionally correct; current snapshot is 47% — NULL is the
dominant producer, llm-stop-hook second, exactly as the baseline stated.)

**Where the NULLs come from** (decomposed):
- **32,645 of 40,802 NULLs (80%) have `scope = 'session-state.watchkeeper'`** and
  `claim_type = 'heartbeat'`. The text is a JSON heartbeat envelope
  (`{"ts":"...","session_id":"wk-session-...`). This is the **watchkeeper-flood class** —
  an external WatchKeeper daemon writing session heartbeats into the claims table (via MCP
  `ingest_claim` with no `source_agent`). These are NOT knowledge claims and should never
  have been in `claims`; they belong in verbatim/spool/session-state storage.
- The remaining ~8,157 NULLs are legitimate-but-unattributed knowledge claims:
  `memoryking-migration` legacy import (879 by citation source), `CLAUDE.md` / `.planning`
  doc ingests from scripts (`scripts/ingest_planning_docs.py:140`,
  `scripts/gitnexus_to_claims.py`), `mcp-session` calls that omitted source_agent (266), and
  the no-source_agent service callers in §1a/§1b (atlas, cli_handlers, operator, llm_steward,
  dream_bridge).

**So NULL is two distinct problems:**
1. **Garbage that should be rejected entirely** (watchkeeper heartbeats, 80% of NULLs).
2. **Real claims missing attribution** (the other 20%) — should be default-tagged, not dropped.

The policy below addresses both: Rule B kills (1), Rule A salvages (2).

---

## 3. Proposed policy rules (safe defaults, additive, configurable)

Policy module proposal: `memorymaster/core/intake_policy.py`, a pure function
`evaluate_intake(*, text, claim_type, subject, scope, source_agent, citations, ...) ->
IntakeDecision(accept|reject, reason, mutated_fields)`. Called from `service.ingest`
immediately **after** `sanitize_claim_input` (so the sacred filter still runs first and is
never gated by policy) and **before** `store.create_claim`. Reject raises `IntakeRejected`
(a `ValueError` subclass) so existing MCP/`try: ... except ValueError` handlers surface it as
a structured `VALIDATION_ERROR` with zero new exception plumbing.

Each rule below is independently togglable; **all defaults preserve current accept behavior
EXCEPT Rule B**, which is the one intentional new rejection (the watchkeeper flood) and is
itself env-gated so it can be disabled if a legit consumer surfaces.

### Rule A — `source_agent` required, with caller-class-aware handling
- **MCP / explicit callers → REJECT** when `source_agent` is empty. The MCP layer already
  defaults to `"mcp-session"` (mcp_server.py:555), so this only bites a *new* caller that
  passes `source_agent=""` deliberately. Explicit API contract: attribute your writes.
- **Hooks / internal extractors → DEFAULT-TAG** to a safe identifier instead of rejecting,
  so a missing tag never silently drops a real learning. Recommended default tag: `"unknown"`
  (so existing NULLs become queryable/auditable, not lost).
- Mechanism: `service.ingest` gains a private notion of "is this an explicit/external call".
  Simplest safe wiring: callers that want strict behavior pass `require_source_agent=True`
  (MCP sets it); everything else default-tags. This keeps the change additive — no caller
  that previously succeeded now fails, except an explicit MCP caller sending an empty agent.
- **Config:** `MEMORYMASTER_INTAKE_REQUIRE_SOURCE_AGENT` (default `"warn"`: tag-to-unknown +
  observability counter; `"strict"`: reject empty for explicit callers; `"off"`: legacy NULL).
  **Safe default `"warn"`** → no rejection vs. today, but NULL becomes `"unknown"` going
  forward, killing the attribution-loss at the source without dropping data.
- **Default tag value:** `MEMORYMASTER_INTAKE_DEFAULT_SOURCE_AGENT` (default `"unknown"`).

### Rule B — reject `session-state*` scope and heartbeat-shaped claims (watchkeeper flood)
- **REJECT** from the `claims` table any claim where ANY of:
  - `scope` matches `session-state` or `session-state.*` (the watchkeeper scope family), OR
  - `claim_type == "heartbeat"`, OR
  - text is heartbeat-shaped: parses as JSON AND contains a `session_id` key AND a `ts`/
    timestamp key with no human-readable claim body (regex/JSON-probe, deterministic, no LLM).
- These belong in **verbatim/spool/session-state storage**, not `claims`. The spool already
  has a `verbatim` op (`spool_drain._replay_verbatim`, line 96) and a dedicated
  `verbatim_memories` table — that is the correct home. Policy here only *blocks the wrong
  door*; routing to the right door is a separate concern (the watchkeeper writer should call
  the verbatim path). For P3, REJECT with a clear reason string telling the caller to use the
  verbatim/session-state store.
- **Config:** `MEMORYMASTER_INTAKE_REJECT_SESSION_STATE` (default `"on"`). This is the ONE
  new default-rejection. Justification for default-on despite "additive" framing: it rejects a
  class that is, by construction, non-claim telemetry (heartbeat JSON, no subject/predicate);
  it raises the bar (allowed by boundary #2) and never flips a prior *accept of a real claim*
  into anything but reject of *telemetry*. Operators who genuinely want heartbeats in claims
  set it to `"off"` (the documented legit-exception opt-out).
- Scope-prefix list configurable: `MEMORYMASTER_INTAKE_REJECTED_SCOPE_PREFIXES`
  (default `"session-state"`).

### Rule C — per-`source_agent` quota per window (configurable, generous default)
- Token-bucket / rolling-count quota keyed on `source_agent`, evaluated **inside
  service.ingest** so it covers ALL chokepoint callers (today the MCP rate limiter at
  mcp_server.py:228 only covers MCP). Reuse the existing token-bucket design (refill per
  minute + a global aggregate bucket so rotating source_agent can't bypass — pattern already
  proven at mcp_server.py:235-263).
- **Config:** `MEMORYMASTER_INTAKE_QUOTA_PER_AGENT_PER_DAY` (default `0` = unlimited =
  current behavior; **safe default is OFF** so nothing that passes today is throttled).
  A generous recommended value for ops to set is e.g. `5000`/agent/day. Window configurable
  via `MEMORYMASTER_INTAKE_QUOTA_WINDOW` (`day`|`hour`|`cycle`, default `day`).
- Exempt `db_merge`/replay (see §4). The global aggregate cap is also default-off.

### Rule D — max distilled claims per stop-hook invocation (≤3 norm)
- The documented norm is **max 3 learnings per Stop** (CLAUDE.md "extracts max 3 learnings";
  enforced today only by the prompt + `[:3]` slice at auto-ingest hook line 146). Make it a
  policy invariant so a misbehaving/edited hook or a compromised LLM response can't flood.
- Enforced as a **per-invocation counter** the hook passes through (e.g. an
  `intake_batch_id` + `intake_batch_max`), or, simpler and chokepoint-local: a per-
  `source_agent`+per-short-window cap of N for agents whose source_agent ends in
  `-stop-hook`/`stop-hook`. Recommend the explicit batch-id approach to avoid coupling to
  agent-name strings.
- **Config:** `MEMORYMASTER_INTAKE_MAX_PER_STOP` (default `3`). Because real hooks already
  cap at 3, **default 3 is non-breaking**; set higher to relax. `0` = unlimited.

### Cross-cutting
- All rejections emit a `policy_decision` event (the `record_event` plumbing already exists,
  service.py:520) + an observability counter (mirror `bump_claim_filtered_findings`), so the
  flood is *measurable* after shipping.
- Order in `service.ingest`: (1) empty-text check [existing] → (2) dedup [existing] →
  (3) `sanitize_claim_input` SACRED [existing, unchanged] → (4) **NEW `evaluate_intake`** →
  (5) `store.create_claim` [existing]. Policy after sanitize guarantees the filter is never
  weakened or skipped by policy.

---

## 4. Risk notes & mitigations

1. **`db_merge` re-ingest must NOT be rejected.** `db_merge.py:257` copies rows verbatim
   (including original `source_agent`) and does NOT call `service.ingest`, so it already
   bypasses the chokepoint policy — *good*, it stays exempt by construction. Mitigation: do
   NOT add policy at `store.create_claim` without an explicit `from_merge=True` exempt flag;
   keep policy in `service.ingest` only for P3. A db_merge claim that legitimately carries
   `scope='session-state.watchkeeper'` from another node would still land — acceptable for
   P3 (merge fidelity > flood prevention on the merge path); revisit if merge re-floods.

2. **Spool replay goes through `service.ingest` → inherits policy.** Verified:
   `spool_drain._replay_ingest` (line 88) calls `svc.ingest`, and `_INGEST_FIELDS` (line 46)
   includes `source_agent` (line 57), so replayed claims keep attribution and are subject to
   Rules A–D. This is CORRECT and desired — a watchkeeper heartbeat that was spooled will now
   be rejected on drain (Rule B). Risk: a backlog of already-spooled heartbeats will produce
   a burst of rejection events on first drain after shipping. Mitigation: rejections are
   counted + logged, not fatal (drain continues per-envelope); pre-existing spooled
   heartbeats simply never reach `claims`. Confirm drain swallows `IntakeRejected` like other
   per-envelope errors (it currently wraps replay in the drain loop — verify the except clause
   catches `ValueError`/`IntakeRejected` so one rejection doesn't abort the batch).

3. **Legit `session-state` consumers.** If any real consumer reads `session-state.*` claims
   from the `claims` table (vs verbatim store), Rule B would starve it. Audit before shipping:
   grep readers of `scope LIKE 'session-state%'`. Current finding: the only producer is the
   external watchkeeper daemon and the data is pure heartbeat telemetry (no subject/predicate,
   JSON body) — no evidence of a claims-table reader. Mitigation: `REJECT_SESSION_STATE="off"`
   opt-out + configurable prefix list; ship Rule B in "warn-count" sub-mode first
   (`session_state` rejections logged but a 1-cycle observation window) if operators want
   proof before enforcing.

4. **Rule A strict mode could break a future MCP caller** that intentionally sends empty
   source_agent. Mitigation: default is `"warn"` (tag-to-unknown), not `"strict"`. No current
   caller passes empty explicitly except the no-source_agent service callers (atlas,
   cli_handlers, operator), which are INTERNAL and will be default-tagged, not rejected.

5. **Quota/max-per-stop false positives** during legitimate bulk imports
   (`scripts/ingest_planning_docs.py`, `gitnexus_to_claims.py`, `memoryking-migration`).
   Mitigation: Rule C and Rule D default to OFF/3 respectively (3 only bites stop-hooks via
   batch-id, not bulk scripts). Bulk importers run under their own source_agent and can be
   allowlisted via `MEMORYMASTER_INTAKE_QUOTA_EXEMPT_AGENTS` (csv, default empty).

6. **Postgres parity.** Policy lives in `service.ingest` (backend-agnostic, above the store),
   so SQLite and Postgres get identical enforcement automatically — no schema change, no
   `schema.sql`/`schema_postgres.sql` edit, satisfying the "never mutate schema" boundary.

7. **Background steward/hooks ingest mid-build.** Policy is pure-Python additive logic in a
   new module imported by `service.ingest`; as long as the new module is importable and
   defaults preserve accept behavior (Rules A/C/D) the live ingest path keeps working. Rule B
   is the only behavior change and is env-gated. Tests must use tmp DBs only.
