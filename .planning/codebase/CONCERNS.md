<!-- doc-head: triaged 2026-08-17 against c832df4 (v4.7.6). Edit body => update this. -->
Active risk register for MemoryMaster, regenerated from the current tree at commit `c832df4`.
Organised around one failure shape: a value or mechanism that reads like it works while doing nothing.
CONFIRMED risks (verified in this tree) are separated from SUSPECTED ones; ordered by blast radius.
Highest-priority: legacy-mode ranking sorts nothing, so the v4.7.6 supersession demotion is inert on the default recall path.
Also covers the never-drained steward proposal queue, the write-only Qdrant index, and lint tooling absent from CI.
Ends with triage of the v3.28.0 (2026-06-09) register: what is fixed, what still holds, what is obsolete.

# Codebase Concerns

**Analysis Date:** 2026-08-17
**Analysed commit:** `c832df4` (main), package version `4.7.6`
**Supersedes:** the v3.28.0 register regenerated 2026-06-09 (triaged at the bottom, not discarded)

## The pattern: mechanisms that read like they work while doing nothing

Nearly every item below is the same failure shape — **a value or a mechanism that returns
success while having no effect**:

- an `ok: true` that means "queued forever" (steward proposals, #3 and #4),
- a `score` that orders nothing (`rank_claim_rows(mode="legacy")`, #1),
- a penalty computed and then discarded (the v4.7.6 supersession demotion, #2),
- a resolution recorded as applied when the apply failed (#3),
- a vector index that is faithfully written and never read (#5),
- linters that are configured, documented, and never executed by CI (#6),
- a boundary test whose tree list omits the tree that violates the boundary (#11),
- 28 `except Exception: pass` sites that turn a broken subsystem into a silent no-op (#7).

None of these fail loudly. Each produces a plausible success signal, which is why several
survived for months. **When reviewing work here, the question is not "does it return
success?" but "what observable thing changes if I delete this line?"**

---

# CONFIRMED — verified in this tree at `c832df4`

## 1. `rank_claim_rows(mode="legacy")` computes a score it never sorts by

- **Files:** `memorymaster/recall/retrieval.py:522-544`, `:421-434`
- **What happens:** the legacy branch builds
  `score = confidence + pinned_bonus + tier_bonus + pending_supersession_penalty` for every
  claim, then returns `apply_session_diversity_cap(rows, cap)[:limit]` — and
  `apply_session_diversity_cap` only filters, never sorts. Final order is whatever the store
  returned: `ORDER BY _fts_rank ASC, pinned DESC, confidence DESC, updated_at DESC, id DESC`
  (`memorymaster/stores/_storage_read.py:351`).
- **Impact:** confidence, pin state, tier and the supersession penalty have **zero effect on
  ordering** in legacy mode. SQLite's bm25 rank is the only live ranking signal.
- **Blast radius — this is the default path.** `query_memory` defaults to
  `retrieval_mode="legacy"` (`surfaces/mcp_server.py:1582`) and the prompt recall hook
  hardcodes it (`recall/context_hook.py:1168`). `_query_legacy_mode` only escapes to hybrid
  when the query contains `" OR "` (`core/service.py:1330`), which single-token hook fan-out
  never produces.
- **Fix approach:** sort by `score` before the diversity cap, or delete the score computation
  in that branch so the inertness is explicit. Sorting is a live recall-quality change — gate
  it and measure against `tests/fixtures/qrels_search.json` first. See also #12: a test
  currently pins this behaviour.

## 2. The v4.7.6 supersession demotion does not fire on the paths it was written for

- **Files:** `memorymaster/recall/retrieval.py:168-230`, `core/service.py:1328`,
  `recall/context_hook.py:1191-1205`, `:1983-2027`
- **What happens:** `pending_supersession_ids` + `_pending_supersession_penalty` (shipped in
  `c832df4`) correctly identify a claim whose supersession is proposed but unapplied — but the
  penalty lands only in `RankedClaim.score`, and (a) legacy mode never sorts by `score` (#1),
  and (b) the prompt hook never reads `row["score"]` at all: it builds its own
  bm25/entity/vector/verbatim/freshness/graph streams and fuses them with RRF.
- **Corroborated by the shipped test:** `tests/test_pending_supersession_ranking.py:113` is
  named `test_legacy_path_scores_the_penalty_even_though_it_does_not_reorder`, and the passing
  case at `:67` is pinned to `mode="hybrid"`.
- **Impact:** the fix is real in hybrid/conversational retrieval and inert in default MCP
  recall and in the per-prompt hook — i.e. inert where an agent actually reads memory. This is
  not "the fix is wrong"; it is "the fix stops one layer short of its consumer."
- **Fix approach:** fix #1 so `score` is authoritative in legacy, or add pending-supersession
  demotion as its own penalty inside `context_hook`'s fusion so the hook path is covered
  independently. Do both if the hook is to be trusted separately from the service.

## 3. Approving a proposal whose apply fails is still recorded as resolved

- **Files:** `memorymaster/govern/steward.py:1401-1432`, `surfaces/mcp_server.py:2572`,
  `recall/retrieval.py:193-205`
- **What happens:** `_apply_steward_approval` can fail; `resolve_steward_proposal` still writes
  the `steward_proposal_approved` audit event and returns
  `{"ok": True, "resolved": True, "applied": False, "apply_error": "..."}`. The MCP tool
  re-wraps that as `{"ok": True, "result": {...}}`, so the top-level signal a caller checks is
  unconditional success.
- **Downstream:** `pending_supersession_ids` treats **any** approved/rejected audit event as
  resolved and never inspects `applied`. A failed apply therefore produces the worst state:
  the claim is not superseded **and** it loses its demotion penalty.
- **Impact:** silently reverts a correction, converting a partial failure into a clean-looking
  success. Worth auditing whether any recent bulk approvals returned `applied: False`.
- **Fix approach:** do not emit the audit event when `applied is False` (or emit a distinct
  `steward_proposal_apply_failed`), and require `payload["applied"] is True` in
  `pending_supersession_ids` before treating a proposal as resolved.

## 4. The steward proposal queue has no automatic drain

- **Files:** `core/service.py:845-999` (the `run_cycle` phase list),
  `surfaces/cli_handlers_curation.py:552`, `recall/retrieval.py:172-175`
- **What happens:** declaring a supersession files a
  `steward_proposal:superseded_candidate` policy event for human review. Nothing drains that
  queue automatically: `run_cycle` runs extractor → dedupe → deterministic → validator → decay
  → compactor → rule mining → skill review, and **`auto_resolver` is not among them** — its
  only in-package caller is a manual CLI curation handler.
- **Reported operational history (team lead, 2026-08-17, not independently re-queried here):**
  249 proposals accumulated, oldest 2026-04-22 (~4 months), and **0**
  `steward_proposal_approved`/`rejected` events existed in the production DB until that date;
  27 `superseded_candidate` proposals were then applied via the official API, leaving ~222
  (170 `stale`, 20 `conflicted`). The shipped code states the same numbers independently:
  "0 of 249 proposals resolved, oldest ~4 months" (`recall/retrieval.py:172-175`).
- **Impact:** any design that assumes a human works this queue is designing on a path with a
  measured execution history of zero. Treat "files a proposal for review" as equivalent to
  "does nothing" until a drain exists.
- **Fix approach:** wire a bounded auto-resolution phase into `run_cycle` for the unambiguous
  kinds, and surface the unresolved-proposal count as a dashboard tripwire so the backlog is
  visible rather than inferred.

## 5. Qdrant is a write-only index: reads are quarantined in code, writes still run

- **Files:** `recall/qdrant_backend.py:442` and `recall/qdrant_recall_fallback.py:242` both
  `raise PermissionError`; `surfaces/mcp_server.py:574-580` rejects the legacy raw entrypoint;
  `recall/planner.py:117-121` silently downgrades a `qdrant` plan to `legacy` with a
  `containment_reason`. Write paths remain live: `qdrant_backend.upsert_claim:233`,
  `delete_claim:278`, `sync_all:475`, plus per-claim sync on every ingest
  (`core/service.py:430-456`) and the daily `govern/jobs/qdrant_reconcile.py` phase.
- **Impact:** the vector layer is continuously maintained and never queried. Cost (embedding,
  upserts, outbox, reconciliation, drift metrics) is paid with no retrieval benefit.
  Retrieval today is lexical + freshness only. `query_memory`'s own docstring says
  `"qdrant": temporarily quarantined; falls back to authoritative lexical search`
  (`mcp_server.py:1583`), and the remaining semantic option, `hybrid`, is documented as
  "slow ~8s" versus legacy's ~0.1s (`:1584`) — so the fast default is also the least
  semantically capable.
- **Planning consequence:** **any plan that assumes semantic recall is live is wrong.**
  Combined with #1, the default path's entire ranking signal is SQLite bm25.
- **Fix approach:** the quarantine is deliberate ("pending authoritative policy rehydration"),
  so the unblock is governed-planner work, not a Qdrant fix. Until then, either finish the
  planner or explicitly decide to keep paying sync cost to preserve the index.

## 6. `ruff` and `mypy` are configured but never run in CI

- **Files:** `pyproject.toml:64` (`[tool.ruff]`), `:68` (`[tool.ruff.lint]`), `:89`
  (`[tool.mypy]`); `.github/workflows/ci.yml` jobs are `test`, `perf`, `release-truth`, `eval`,
  `deployment-smoke` — the only lint step in the whole file is `helm lint` (`ci.yml:149`).
- **Impact:** `ruff check memorymaster/` is listed as a required verification step in
  `AGENTS.md`, but nothing enforces it on push or PR. Lint and type cleanliness are local
  discipline that no gate protects, so drift accumulates invisibly and lands on whoever next
  runs the command.
- **Fix approach:** add a `lint` job running `ruff check` (and `mypy` if the tree is clean
  enough to gate on). Baseline first — measure the current violation count before making it
  blocking, otherwise the job lands red and gets disabled.

## 7. 28 `except Exception: pass` sites turn broken subsystems into silent no-ops

- **Files:** 28 occurrences package-wide; three in the ingest path alone —
  `core/service.py:750` (entity resolution + pattern extraction), `:783` (`entity_id`
  back-fill), `:804` (`claim_ingested` webhook).
- **Impact:** entity enrichment can stop working repo-wide while every ingest still returns a
  healthy `Claim`. Indistinguishable from "the feature is off."
- **Fix approach:** adopt the `run_cycle` phase convention — catch, `logger.warning(...)`, and
  surface the error in the returned payload (`core/service.py:906-908`). "Non-fatal" should
  mean "recorded and visible", not "invisible".

## 8. `pending_supersession_ids` scans a bounded, newest-first event window

- **Files:** `recall/retrieval.py:193`, `:206`, `stores/_storage_read.py:456`
- **What happens:** both scans use `list_events(..., limit=2000)` against events returned
  `ORDER BY created_at DESC, id DESC`. The proposals this targets date to 2026-04-22; as the
  event log grows, the oldest pending proposals fall out of the window and stop being
  penalised — no error, no metric.
- **Impact:** the demotion decays over time in exactly the oldest/most-stale cases it exists to
  cover. Whether the window is already exceeded in production is **not verified here**.
- **Fix approach:** filter `policy_decision` events by `details` in SQL rather than scanning a
  fixed window, or materialise pending proposals into a table.

## 9. Two enforced size budgets sit exactly at their cap

- **Files:** `core/service.py` = 2450 lines (budget ≤ 2450) and `surfaces/dashboard.py` = 1550
  lines (budget ≤ 1550) — `tests/test_architecture_budgets.py:23-24`.
- **Why fragile:** adding a single line to either file fails the suite. Ingest, recall and
  `run_cycle` all live in `core/service.py`, so most memory-behaviour work — including the
  fixes for #1, #2 and #7 — collides with this on the first edit.
- **Safe modification:** extract into a bounded collaborator (`core/services/integration.py`
  is at 269/800; `surfaces/dashboard_commands.py` at 70/800) rather than editing in place.
  Extracted functions are additionally capped at 50 lines (`test_architecture_budgets.py:34-44`).

## 10. Raw SQL against `claims` from the service layer

- **Files:** `core/service.py:776-783` — `UPDATE claims SET entity_id = ? WHERE id = ?` on a
  connection taken from the store.
- **Impact:** bypasses the `_storage_write_claims` mixin, the event ledger, and Postgres
  parity; the same write is untested against `PostgresStore` by construction. It is wrapped in
  a bare `except Exception: pass`, so a Postgres failure here is invisible (compounds #7).
- **Fix approach:** add `set_claim_entity_id` to `stores/_storage_write_claims.py` and call it.

## 11. `PostgresStore` inherits from `SQLiteStore`, and the boundary test omits `surfaces`

- **Files:** `stores/postgres_store.py:158`; `tests/test_extension_boundaries.py:15-19`
- **Parity by inheritance:** there is no store ABC — callers probe capabilities with `hasattr`
  (`core/service.py:629`, `:1497`). A new SQLite-only method is silently inherited by Postgres
  and may fail only at runtime, on the team backend, under load. Every new store method needs
  a Postgres parity test; `tests/test_postgres_*_boundary.py` is the pattern.
- **Boundary gap:** `CORE_TREES = ("core", "govern", "recall", "stores")` and
  `OPTIONAL_PREFIXES` covers only `bridges`, `knowledge.wiki`, `knowledge.vault`. The
  documented rule that nothing imports *from* `surfaces` (`surfaces/__init__.py:1-7`) is
  untested — and already violated at `core/service.py:1796`
  (lazy import of `surfaces.session_tracker`). The architecture drifts invisibly because the
  test passes.
- **Fix approach:** add `surfaces` to the forbidden prefixes; inject the tracker from the
  surface or move the primitive into `core/session_scope.py`.

## 12. The legacy ranking defect is pinned by a test rather than fixed

- **Files:** `tests/test_pending_supersession_ranking.py:113`
- **Risk:** the test documents and locks in the behaviour from #1/#2. Anyone fixing the sort
  will see this test fail and may "fix" the test instead of the code. If #1 is addressed, this
  test must be rewritten in the same change with a pointer to this entry.
- **Priority:** High — it is the tripwire guarding the highest-blast-radius item here.

---

# SUSPECTED — plausible, not proven in this tree

## S1. Multi-process SQLite write concurrency (carried from v3.28.0 #1, materially reduced)

- The 2026-06-05 corruption event was real, but the largest writer has since been removed from
  the write path: the recall hook now constructs its service with `read_only=True`
  (`recall/context_hook.py:1355`) and spools access/feedback signals for later replay
  (`govern/jobs/spool_drain.py`), `busy_timeout` is unified at 15000 ms default across every
  connection (`stores/_storage_shared.py:104-130`, replacing the divergent 0/5000/30000 ms
  sites), and an integrity phase runs checkpoint/quick_check/fk_check/VACUUM INTO each cycle
  (`govern/jobs/integrity.py`, `fk_repair.py`).
- **Still unproven:** N independent ingest/steward processes on one large file remain
  possible; no single-writer funnel exists. `surfaces/mcp_http.py` now offers a shared-process
  MCP option that could serve as one, but nothing requires its use.
- **Why SUSPECTED not CONFIRMED:** I did not reproduce corruption or measure concurrent writer
  counts. Treat as materially mitigated, not closed.

## S2. Intake-vs-steward throughput imbalance (carried from v3.28.0 #2)

- Structurally unchanged: hooks ingest candidates per prompt across many panes; the steward
  runs on cron. `batch_limit` now threads through every cycle job (default 200 in
  `core/service.py:852`, and the shipped hook passes 2000 —
  `config_templates/hooks/memorymaster-steward-cycle.py:41`), which is a real mitigation.
- A live observation is recorded in-code: "21k claims eligible-for-core but only ~7k actually
  core, peripheral empty" (`core/service.py:988-992`), which prompted adding `recompute_tiers`
  to every cycle. That suggests backlog effects were real recently.
- **Why SUSPECTED:** current candidate/confirmed ratios were not measured here.

## S3. Stale pane code against a migrated schema (carried from v3.28.0 #5)

- Migrations apply on connect (`stores/migrations/runner.py`), but a long-lived MCP process
  keeps running the code it started with; there is no version handshake. Unverified whether
  this has caused an incident.

## S4. Minor carried-over items

- `MEMORYMASTER_ALLOW_SENSITIVE_BYPASS: "1"` is still set in CI (`.github/workflows/ci.yml:30`)
  — **CONFIRMED still present**; the risk that it leaks into production remains theoretical,
  and default-off is test-asserted in `tests/test_security_access.py`.
- Key rotation under burst load (`core/key_rotator.py`, `core/llm_budget.py`) and external key
  management for payload encryption — not re-verified this pass.
- Context-packing token counts as estimates rather than a real tokenizer — not re-verified.

---

# Triage of the v3.28.0 register (2026-06-09)

| v3.28.0 concern | Status at `c832df4` | Evidence |
|---|---|---|
| #1 Concurrent per-pane writers, corruption has happened | **MATERIALLY MITIGATED, not closed** | Recall hook is `read_only=True` (`context_hook.py:1355`) + spool replay; unified 15000 ms `busy_timeout` (`_storage_shared.py:104-130`); per-cycle integrity/fk_repair phases. No single-writer funnel. Carried as **S1**. |
| #2 Intake-vs-steward backlog | **STILL TRUE, partly mitigated** | `batch_limit` threads through all cycle jobs; hook passes 2000. Carried as **S2**. |
| #3 Qdrant sync fire-and-forget, no reconciliation | **FIXED — then superseded** | `govern/jobs/qdrant_reconcile.py` gives a drift metric, `MEMORYMASTER_QDRANT_DRIFT_MAX` threshold, daily throttle, and convergence, wired at `core/service.py:955`. `_qdrant_sync` no longer swallows silently — it logs and enqueues to a durable outbox (`core/service.py:430-456`). Both halves closed. **But the index is now write-only** — see #5. |
| #4 Flat-module sprawl, ~110 top-level modules | **FIXED (P2 restructure)** | The package is now `core/ stores/ recall/ govern/ surfaces/ knowledge/ bridges/` plus feature packages; the 109 root modules are deprecated `sys.modules` shims (`memorymaster/service.py:1-11`) with a dated removal gate (`docs/compatibility.md`, pinned by `tests/test_architecture_budgets.py:64`). The predicted "eventual subpackage refactor" happened. |
| #5 Per-pane MCP process footprint; stale code vs migrated schema | **PARTLY ADDRESSED** | `surfaces/mcp_http.py` now offers a shared authenticated HTTP server, so a single shared process is possible; nothing requires it, and no version handshake exists. Carried as **S3**. |
| #6 Carried-over minor concerns | **MIXED** | CI sensitive-bypass env var confirmed still present (`ci.yml:30`); the rest not re-verified. Carried as **S4**. |
| v2.0-era: no migration framework | **STILL FIXED** | `stores/migrations/` with `runner.py`, checksum bookkeeping, drift detection; 20 versioned migrations. |
| v2.0-era: SQLite/Postgres schema drift | **STILL FIXED** | Every migration ships `apply_sqlite` + `apply_postgres`; `tests/test_postgres_*_boundary.py` family. Note the new inheritance-shaped parity risk in #11. |
| v2.0-era: zero-dependency core has hidden coupling | **OBSOLETE** (unchanged) | Core is not zero-dep; optional extras are runtime-gated. |

---

# Priority summary

| # | Concern | Confidence | Blast radius |
|---|---|---|---|
| 1 | Legacy-mode score orders nothing | CONFIRMED | Default MCP recall + prompt hook |
| 2 | Supersession demotion inert on default paths | CONFIRMED | Every agent read of memory |
| 3 | Failed apply recorded as resolved | CONFIRMED | Silently reverts corrections |
| 4 | Proposal queue has no automatic drain | CONFIRMED | All governance that "files for review" |
| 5 | Qdrant is a write-only index | CONFIRMED | Recall quality ceiling + ongoing wasted cost |
| 6 | ruff/mypy configured, never run in CI | CONFIRMED | Whole-repo code quality drift |
| 7 | 28 silent `except…pass` | CONFIRMED | Package-wide observability |
| 8 | Bounded event-window scan | CONFIRMED (code) | Demotion decays as the log grows |
| 12 | Defect pinned by a test | CONFIRMED | Guards #1 |
| 9 | Budgets exactly at cap | CONFIRMED | Blocks fixes for #1, #2, #7 |
| 10 | Raw SQL bypassing the store | CONFIRMED | Postgres parity |
| 11 | Store parity by inheritance; boundary test omits `surfaces` | CONFIRMED | Team backend + architecture drift |
| S1 | Multi-process SQLite writes | SUSPECTED | Data integrity (mitigated) |
| S2 | Intake/steward imbalance | SUSPECTED | Recall precision, DB growth |
| S3 | Stale pane code vs migrated schema | SUSPECTED | Rare, hard to diagnose |
| S4 | Minor carried-over items | MIXED | Low |

---

*Concerns audit: 2026-08-17 against `c832df4` (v4.7.6)*
