# Inert-signal remediation — roadmap

> **The shape.** Every item here is one failure: *a value or mechanism that reads
> like it works while doing nothing.* Five of them are the same substitution —
> a counter meaning **"the machine ran"** being read as **"the work happened"**.
> `discovery_queued`, `status=completed`, `ok:True`, `provider_failures=0` and
> suppressed writes all say something executed; none says anything happened.

Audit: 2026-08-18 against `main` @ `8227fc0`, quantified on the production DB
(131,113 claims / 2,410,028 events). Two instances were already fixed and are
listed at the bottom for context — including one where the *fix itself* was an
instance of the class.

**Rule for every item:** the regression test must fail without the fix. A test
that passes either way proves nothing — that is how the first supersession fix
shipped inert.

---

## Queue

- [x] **R1 — Health check sees ~14 minutes of history** · HIGH · ✅ done
  `govern/operational_health.py:62-66` scans `list_events(limit=1000)` with **no
  `event_type` filter** against 2.4M events. Measured window: **13.9 min**
  (01:54:41 → 02:08:38). A provider outage 20 minutes old is structurally
  invisible and `evaluate_operational_health` reports healthy.
  *Why it matters most:* this is the layer meant to catch the others.
  **Fixed:** `list_events` gained a `since` bound on BOTH backends (SQLite +
  the explicit Postgres override) — the shared primitive R4 also needs. The
  check now scans a declared 24h window and returns `provider_failure_scan`
  with the window, rows examined and oldest event seen, so a `0` can no longer
  read as "all clear" when it means "saw almost nothing".
  *Also found:* **no code path anywhere records a provider-failure event**, so
  the count could never have fired regardless of window. Recorded under R10.
  4 tests, verified failing without the fix.

- [ ] **R2 — A failed apply is latched permanently** · HIGH · small
  `govern/steward.py:1407-1421` writes `steward_proposal_approved` regardless of
  `applied`. Three consumers read "an approved event exists" as resolved and
  none reads `payload["applied"]`: `list_steward_proposals:1259-1264` (drops it
  from the queue), `recall/retrieval.py:193-205` (lifts the −0.40 demotion), and
  `resolve_steward_proposal:1383-1389` — whose `already_resolved` short-circuit
  **refuses the retry**. Result: claim not superseded + demotion lifted + gone
  from the queue + unrecoverable through the API.
  **Generator still live:** `govern/ingest_governance.py:159-168` dedupes per
  *(target, replacement)* pair, not per target, so N replacements against one
  target file N proposals and N−1 fail by construction. 1 real case today
  (claim 123737, two dream-worker proposals 1s apart).
  **Fix:** only record approval when applied (or a distinct
  `steward_proposal_apply_failed`), teach the three consumers to read `applied`,
  make the dedup target-scoped.

- [ ] **R3 — The validator promotes claims that are pending supersession** · MED
  The proposal is a `policy_decision` event; the validator never reads it, so a
  claim declared outdated gets independently promoted to `confirmed`.
  **21 of 59** proposals were followed by exactly that. Claim 130542 spent ~11h
  at `confirmed` *after* being declared superseded.
  **Fix:** check for an unresolved `superseded_candidate` before promoting.

- [ ] **R4 — `limit=`-bounded event scans, a family** · MED · shared helper
  Same shape as R1, three more sites. `govern/steward.py:1250-1251` is the
  tightest: `policy_decision` is at **362 of 600 — 60% consumed**. When it
  overflows the **oldest** pending proposals vanish first (the ones most needing
  review); if resolutions fall out of the audit side, resolved proposals
  reappear as pending and a re-approval lands straight in R2's latch.
  `recall/retrieval.py:193,206` (limit=2000) is safe today but fails in both
  directions, and its `except Exception: ids = set()` makes a fault
  indistinguishable from "nothing pending".
  `core/lifecycle.py:46` is correctly bounded — **leave it alone.**
  **Fix:** one shared helper: filter by type + time bound, no global row cap.

- [ ] **R5 — 2,856 discovery jobs "completed", 2 observations ever** · MED
  `knowledge/graph_observations.py:247-248` returns an empty result with no
  diagnostics for an empty scope, and `graph_observation_engine.py:191-196`
  calls `complete_job` regardless — `completed` collapses "synthesized" and
  "found nothing". 2,846 completed with zero diagnostics. `complete_job`
  (`graph_observation_repository.py:262`) then **hashes** the diagnostic codes,
  destroying the "why" at write time.
  *Scope note:* the underlying data thinness (only scope `user` has eligible
  supports; 308 of 131,113 claims carry an evidence link; the
  `ontology_version` filter excludes half the edges) is **structural and out of
  scope here** — this item fixes the *signal*, so the thinness becomes visible.
  **Fix:** distinct terminal state for "nothing found", store diagnostic codes
  as text.

- [ ] **R6 — Postgres: four sites of one silent-dropper, fixed once** · MED
  `PostgresStore(SQLiteStore)` inherits `?`-placeholder SQL that psycopg
  rejects, and every caller suppresses the exception. The project already fixed
  this once — `postgres_store.py:2471-2478` documents it verbatim for
  `recompute_tiers`. Standing sites: `_storage_lifecycle.py:727` / `:738`
  (`record_access`, caller wraps in `contextlib.suppress`), `core/service.py:777-784`
  (raw `UPDATE claims` from the service layer), and `core/service.py:714-751`
  (passes a connection into `entity_registry` functions typed
  `conn: sqlite3.Connection`).
  *Compounding:* `recompute_tiers`, now fixed, runs on permanently-zero
  `access_count` and confidently demotes the aging corpus to `peripheral`.
  **Only bites Postgres deployments** (this operator runs SQLite).
  **Fix:** small per site + one test that pins the pattern.

- [ ] **R7 — `surfaces` boundary is a docstring, not a guard** · LOW · one line
  `surfaces/__init__.py:4-6` asserts zero internal fan-in; `core/service.py:1796`
  lazily imports `surfaces.session_tracker`. **Correction to an earlier
  framing:** `tests/test_extension_boundaries.py` is *not* a guard with a hole —
  it is scoped to optional companions and never claimed `surfaces`. The defect
  is an invariant with no enforcement anywhere. Milder than the rest: nothing
  reports success while doing nothing.
  **Fix:** drop the import or extend the boundary test to cover `surfaces`.

- [ ] **R8 — `_llm_rerank_enabled` fails OPEN** · LOW
  `core/service.py:262-270`: `except Exception: return True` reports the feature
  enabled when the import fails. Every other gate in that function fails closed.
  **Fix:** fail closed, one line.

- [ ] **R9 — 488k events recording that nothing changed** · MED · *(new)*
  `deterministic_adjust=+0.000` accounts for **487,927** of 2.41M events — ~20%
  of the entire log is "I looked and changed nothing", written ~15k/day. This is
  the flood that collapses R1's and R4's windows, and it is itself the pattern:
  a write whose content is the absence of an effect.
  **Fix:** don't record a no-op adjustment (keep the counter, drop the row), or
  record it at a coarser granularity.

- [ ] **R10 — The provider-failure signal has no producer** · MED · *(found while fixing R1)*
  A repo-wide sweep finds **no `record_event` anywhere** that writes a
  provider failure; `core/llm_provider.py` only logs warnings on timeout,
  non-zero exit and OS error, leaving no queryable trace. So R1's check was
  doubly inert — wrong window *and* nothing to find. Only 4 events in 2.4M
  match the pattern at all, and they are `transition` rows.
  **Fix:** record the failure where it happens. `llm_provider` has no store
  handle, so either surface it through `observability.bump_counter` (in-process,
  exposed on the metrics endpoint) or have the calling layer record an event.

---

## Out of scope (recorded, deliberately not fixed here)

- **Graph evidence-link thinness** — 0.23% of claims carry an evidence link and
  the `ontology_version` filter drops half the edges. Structural; R5 only makes
  it visible.
- **Writers with zero production rows** — `quality_scores`, `rule_stats`,
  `miner_state`, `contradiction_verdicts`, `action_proposals`,
  `qdrant_sync_state`. `rule_stats` is notable: its bootstrap is default-ON yet
  empty. Each needs its own trace before anyone claims it is broken.
- **Qdrant is a write-only index** — reads raise `PermissionError`
  (`qdrant_backend.py:442`, `qdrant_recall_fallback.py:242`) while every write
  path still runs. Cost paid continuously, zero retrieval benefit. A product
  decision, not a bug.

## Verified clean — do not re-audit

- `govern/jobs/qdrant_reconcile.py:153-167` — reports `{"skipped": …}` /
  `{"error": …}` explicitly instead of a bare success. **The model the rest
  should copy.**
- `core/lifecycle.py:46` — correctly bounded scan.
- `core/webhook.py:57-74` — returns a bool and logs failures; the
  `except Exception: pass` around it is redundant, not a silent drop.
- Supersession proposals target the OLD claim, not the replacement (verified
  against production payloads) — no inversion bug.

## Already fixed (context)

- [x] **Declaring a supersession did nothing** — accepted, answered `ok` +
  "queued for steward review", filed into a queue where **0 of 249** proposals
  had ever been resolved in ~4 months. Fixed in `c832df4` by demoting at
  proposal time. Root cause of the drain never running: `auto_resolver` is not a
  `run_cycle` phase; its only in-package caller is a manual CLI handler.
- [x] **…and that fix was itself inert** — it folded a penalty into `score`,
  which the `legacy` branch never sorts by, and legacy is the default on every
  path that matters. Proven with a test pinned to `mode="hybrid"`. Fixed in
  `8227fc0` with a stable partition, and this time verified to fail without the
  fix. **This is why the rule at the top exists.**
