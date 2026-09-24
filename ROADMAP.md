<!-- doc-head: sole roadmap; 4.9.0 live Jev decisions + weekly-review cure in progress (checklist under Now) -->
# MemoryMaster roadmap
# Covers: live Jev decision ledger (4.9.0 checklist), useful-memory delivery/evaluation, workflow analytics, governed observations and deferrals.
# Key terms: Workflow Intelligence, governed skills, graph observations, user profile, sustainability.
# Read when: choosing release scope, accepting a feature, or checking deferrals.
# Authority: sole roadmap; planning ledgers implement it and never replace it.
# Safety: SQLite remains authority; generated user.md is disposable and feature-off by default.
<!-- /doc-head -->

## Shipped in v4.6.0

- The personal/local SQLite profile and versioned `remember / recall / forget /
  improve` Python, CLI, and MCP facade are public.
- Unified bounded capture, exact source -> evidence -> claim -> graph lineage,
  replay-safe background jobs, and the `personal-v1` ontology are implemented.
- Trusted graph traversal requires active authorized supporting claims and
  citations; candidate promotion remains steward-controlled.
- Capture Inbox, deterministic demo, clean package profiles, supply-chain
  evidence, LongMemEval gates, and comparable OAuth-backed QA are complete.

## TencentDB Agent Memory v2.0 adoption boundary

Reviewed upstream `TencentCloud/TencentDB-Agent-Memory` at commit
`fe3230f176f1bf5832fee79d12494bbc2d19a8aa` (2026-08-06). MemoryMaster adopts
useful product patterns without importing Tencent runtime code or replacing
its governed-claims authority:

| Tencent pattern | MemoryMaster decision |
|---|---|
| Session/project isolation | Adopted as explicit durable session bindings; no inferred `global`. |
| Hermes memory integration | Adopted through the native Hermes `MemoryProvider`, authenticated MCP/HTTP authority, durable replay outbox, and read-only replica fallback. |
| Reusable skill memory | Adopted as evidence-linked `personal-skill-v1` candidates, human-only promotion, immutable versions, and confirmed scoped skill recall. |
| Per-turn matched skill injection | Implemented locally as a bounded `APPROVED SKILLS` recall section; candidate, stale, and unauthorized skills are excluded. |
| Chat memory, Wiki, and graph assets | Retain MemoryMaster source/evidence/claim capture, opt-in wiki projection, and claim-supported entity graph rather than adding parallel authorities. |
| Memory Hub, loadouts, team ACLs, proxy replacement, cloud database | Deferred: no personal SQLite requirement justifies multi-user/cloud infrastructure or a second agent gateway. |

## Now

### Live Jev decisions and weekly-review cure (4.9.0, operator order 2026-09-23)

Operator order: execute the 2026-09-23 plan end to end and turn TypeSafe Jev on
**live in production without a smoke-test phase**, logging every decision and
outcome for calibration, off-policy evaluation and RL. Contract:
`.planning/JEV-LIVE-4.9.0.md`; findings: `.planning/audits/2026-09-23-weekly-claude-review/REPORT.md`;
design rationale: `artifacts/2026-09-23-plan-jev-y-curacion.html` (local).
A box is ticked only with evidence (command, count or receipt) in the ledger.

**Phase 0 — operator configuration (2026-09-23)**
- [x] F-01 project `.mcp.json` uses `-I` + `local-trusted`; import from repo cwd resolves to site-packages.
- [x] F-02 `MEMORYMASTER_JEV_INGEST_SHADOW` removed from settings; observer block removed from installed Stop hook; hook still returns `{"decision":"approve"}`.
- [x] F-09 third-party `agent-skills@addy-agent-skills` plugin disabled (source of the SessionStart JSON error).
- [x] F-19 Hermes identity moved from the user environment to `HKCU\Software\MemoryMaster\HermesMcp`; Hermes task restarted; health 200 / ready 200 / unauth 401 / tools 51.
- [x] TypeSafe key valid in every copy (HTTP 200, `jev-1.13.0`); earlier 403s were transient.
- [x] Backups of every touched file in `~/.memorymaster/backups/phase0-20260923/`.
- [x] Reconnect the MemoryMaster pane MCP (`/mcp`) so it loads the installed package (operator, 2026-09-23; orphan server process stopped).

**Phase 1 — security and correctness (branch `release/4.9.0-jev-live`)**
- [x] F-04 structural provider-output errors are bounded (`55a5871`; repro 6 → 2 paid calls).
- [x] F-04b failed provider calls record estimated input tokens — source-tested, wave 1 (merged `30dbce2`; independent verifier PASS; non-ML gate 5641 passed).
- [x] F-05 semantic-failure counter resets only on success; absolute error cap (`MEMORYMASTER_DREAM_MAX_CAPTURE_ERRORS`=8) — source-tested, wave 1 (merged `30dbce2`; independent verifier PASS; non-ML gate 5641 passed).
- [x] F-06 packed consolidation batches; isolate per capture only after a failed packed call — source-tested, wave 1 (merged `30dbce2`; independent verifier PASS; non-ML gate 5641 passed).
- [x] F-07 Dreaming lease renewed per batch with ownership check (`DreamLeaseLost`) — source-tested, wave 1 (merged `30dbce2`; independent verifier PASS; non-ML gate 5641 passed).
- [x] F-10/F-15/F-16 one in-process transport and one egress redactor (decisions core); JEV key never reachable from cwd modules — source-tested, wave 1 (merged `30dbce2`; independent verifier PASS; non-ML gate 5641 passed). Residual planted `httpx.py` removed by the R6 stdlib transport (wave 3).
- [x] F-11 normalized, tenant-bound session key (`recall/jev_surfaces.decision_session_key`) — source-tested, wave 1 (merged `30dbce2`; independent verifier PASS; non-ML gate 5641 passed).
- [x] F-13 `status_in` pushed into SQL paging (SQLite + Postgres) — source-tested, wave 1 (merged `30dbce2`; independent verifier PASS; non-ML gate 5641 passed).
- [x] F-14 lexical stream pages until 60 authorized rows — source-tested, wave 1 (merged `30dbce2`; independent verifier PASS; non-ML gate 5641 passed).
- [x] F-17 sentence-transformers imported only when the provider needs it (`MEMORYMASTER_EMBEDDING_PROVIDER`) — source-tested, wave 1 (merged `30dbce2`; independent verifier PASS; non-ML gate 5641 passed).
- [x] F-18 `PRAGMA optimize` after migrations — source-tested, wave 1 (merged `30dbce2`; independent verifier PASS; non-ML gate 5641 passed).
- [x] F-20 `scheduled_archive` filters in SQL and requires a recorded "no longer useful" judgment (`decisions/archive_gate.py`) — source-tested, wave 1 (merged `30dbce2`; independent verifier PASS; non-ML gate 5641 passed).
- [x] F-21 explicit operator/automation actor on proposal resolution; Jev proposals never auto-approved — source-tested, wave 1 (merged `30dbce2`; independent verifier PASS; non-ML gate 5641 passed). MCP refusal of Jev proposals: wave 3.
- [x] F-12 several retrieval canaries in the operational review — source-tested, wave 1 (merged `30dbce2`; independent verifier PASS; non-ML gate 5641 passed).
- [x] F-03 compiled profile fed from confirmed claims and Dreaming when verbatim is empty; freshness check; dated hook log — source-tested, wave 1 (merged `30dbce2`; independent verifier PASS; non-ML gate 5641 passed).
- [x] All review repro scripts encoded as regression tests (red on 6909841, green on the branch; red-first confirmed by the wave verifiers): F-04..F-07 `test_dreaming_worker.py`; F-10/F-15/F-16 `test_decisions_transport.py`, `test_decisions_egress.py`, `test_jev_selector.py`; F-11 `test_session_key_normalization.py`; F-13 `test_candidate_pool_status_pushdown.py`; F-14 `test_lexical_stream_authorized_paging.py`; F-17 `test_mcp_stdio_embedding_provider.py`; F-18 `test_skill_catalog_plan_after_migrations.py`; F-20 `test_scheduled_archive_judgment_gate.py`; F-21 `test_close_settled_proposals_actor.py`. Review-B targeted the removed ingest-shadow observer (F-02, a configuration fix; that code is not in the release).

**Phase 2 — decisions engine, ledger and metrics**
- [x] `memorymaster/decisions/` (config, credentials, egress, transport, questions, ledger, policy, engine, outcomes, metrics, export) — source-tested, wave 1 (merged `30dbce2`; independent verifier PASS; non-ML gate 5641 passed).
- [x] S1 REVALIDATE: stale claims re-judged; re-confirmation live; backlog CLI — source-tested, wave 2 track gov (`6e617aa`, verifier PASS, 925 tests; integrity-freeze and pending-supersession guards).
- [x] S2 RECALL: per-candidate relevance/evidence/contradiction/instruction questions; adaptive k; 10 % logged exploration; exposure and delivery logged — source-tested, wave 2 track recallsurf (`a3defc8`, verifier PASS, 1235 tests).
- [x] S3 INGEST: eight source-review checks as nouls; `held` instead of discard; 5 % exploration — source-tested, wave 2 track ingest (`8239009`, verifier PASS, 624 tests; rulings R1/R2).
- [x] S4 DEDUP: duplicate/contradiction/supersession proposals, no status change — source-tested, wave 2 track gov (`6e617aa`).
- [x] S5 SKILLS: selector on the shared engine and transport, no subprocess — source-tested, wave 2 track skillshints (`bc46864`, verifier PASS, 613 tests).
- [x] S6 HINTS: prompt signal nouls with regex fallback; off mode byte-identical over 400 cases — source-tested, wave 2 track skillshints (`bc46864`).
- [x] S7 ROUTE: retrieval-mode choice with keyword fallback (context_hook auto-classify sites only) — source-tested, wave 2 track recallsurf (`a3defc8`).
- [x] S8 SESSION: session-start injection ranked from confirmed claims only — source-tested, wave 2 track recallsurf (`a3defc8`).
- [x] Outcome joiners: turn usage in Stop, lifecycle tail, skill invocation, held release — source-tested, wave 2 (recallsurf, gov, ingest).
- [x] Dashboard "Decisions" tab and operational-review `jev_decisions` check — source-tested, wave 2 track ops (`2208d73`; headless browser at 1280/390 px, operator-role CSRF writes).
- [x] Calibration report, off-policy evaluation, training export and weekly review-queue scripts — source-tested, wave 2 track ops (`2208d73`); cp1252 CLI crash fixed in `de4ffe1`; one untrusted-label set in `166ea91`.

**Queued — GraphRAG vector-first recall (A2A request from codex-memorymaster, operator-approved 2026-09-23; starts after the recall track merges)**
- [x] Reproduce a real failure of the current `_enrich_with_entity_graph` (capitalization-only seeds) — 42 of 48 new tests red on base (`15b25af`).
- [x] Opt-in mode: authorized lexical/semantic claim IDs as graph seeds, 1-2 hops with fanout/candidate/token caps, every claim and support rehydrated and authorized in SQLite, generated observations never self-evidence, base results never fully displaced, fallback on no seeds/error/timeout — `MEMORYMASTER_RECALL_GRAPH_MODE`, off by default; also fixed a private-support leak in entity enrichment.
- [x] Red-then-green tests: lowercase queries, retired/out-of-scope/tenant/principal claims, invalid support/citation, expansion caps, fallback, ordinary-recall non-regression (11 of 14 verifier mutants killed; the two gaps are closed in wave 3).
- [x] Frozen cohort of real relational + ordinary questions (no recycled 953 labels): 120 questions; oracle coverage 0.0, so hit@5 and precision@5 are UNMEASURED; 0 confirmed-to-confirmed edges; p50/p95 baseline 88/302 ms vs vector_first 91.5/296 ms.
- [x] Independent security/integrity review; not enabled in production by default — wave-2 and wave-3 verifiers ok; found and fixed a pre-existing private-support leak in entity enrichment (`ab468d3`); report `.planning/audits/2026-09-23-graphrag/REPORT.md` (local).

**Wave 3 — pre-activation fixes found by the wave-2 verifiers (2026-09-23)**
- [x] R6 stdlib TLS transport (cold hook 1.0–1.25 s with httpx vs 0.43–0.69 s measured with `http.client` + system trust store); httpx no longer a core dependency — source-tested, wave 3 (merged `e597baa`; independent verifier PASS; loopback cold process 162–213 ms, httpx/certifi never imported).
- [x] Hook decisions hard-bounded (locked ledger, 250 ms busy timeout); never act on an unlogged decision; breaker-open probes instead of sending every call; public `record_skip` API — source-tested, wave 3 (merged `e597baa`; independent verifier PASS after the orchestrator's send-intent fix `f4096fb`: under a held write lock 10 sends → 0, 0.51 s per hook). S2/S8 skip rows `025d40b`; orphan intents counted as spend and warned `c04126c`.
- [x] Egress: home paths in URL/UNC/NAS/percent-encoded/relative spellings, apostrophe usernames, phone numbers — source-tested, wave 3 (merged `e597baa`; independent verifier PASS; verifier probe 0 leaks in the documented spellings; remaining gaps listed in docs/jev-decisions.md).
- [x] R8 S3 questions for personal-scope candidates (v2 privacy/usefulness, personal scope variant) — source-tested, wave 3 (merged `e597baa`; independent verifier PASS).
- [x] B1 passthrough: authorized private/sensitive rows stay in live recall output and are never sent; sensitive claims never asked in S1/S4/S8 — source-tested, wave 3 (merged `621dc1b`; independent verifier PASS).
- [x] S4 only in the steward cycle (never inside `run_cycle`); S1 fails closed on a supersession scan fault; MCP cannot resolve Jev proposals — source-tested, wave 3 (merged `621dc1b`; independent verifier PASS).
- [x] R7 held-candidate retention bound (30 days, quarantined never exempt); S3 rewards joined to lifecycle; R1 covers Gemini FAILED_PRECONDITION — source-tested, wave 3 (merged `621dc1b`; independent verifier PASS).
- [x] Metrics/review/queue performance (<2–3 s on 4000×80); silent-surface rule per surface kind; OPE treats fallback rows as legacy targets; skip rows — source-tested, wave 3 (merged `621dc1b`; independent verifier PASS; untraced 0.9 / 0.9 / 0.46 s; CI skips the machine-bound timing budgets `d3bf664`).
- [x] `core/service.py` back under its 2450-line budget (2377); GraphRAG regression gaps closed (`dd25566`, `196e01d`); GraphRAG report written — source-tested, wave 3 (merged `621dc1b`; independent verifier PASS; A2A result sent to codex-memorymaster).

**Phase 3 — live activation (no smoke test)**
- [x] Full non-ML gate on the release revision: 6668 passed, 1 failed (`87097f5`; the failure was the repo guard against bare `sqlite3.connect`, fixed in `fb9d776`).
- [x] 4.9.0 wheel installed in both runtimes, file-identical (434 files, 0 mismatches; built from `2bd8264`); rollback 4.8.9 wheel kept; consistent 7.2 GB pre-install DB backup; migrations 0026/0027 applied 2026-09-23T23:40Z.
- [x] Installed hooks ported with backups (`~/.memorymaster/backups/hooks-pre490-20260923T233944Z`): recall/steward-cycle patched in place (operator look-ahead briefing, provider and dedupe settings kept), session-start/classify/auto-ingest from templates; every hook keeps `sys.path.append` so the installed package wins over the older checkout.
- [x] `MEMORYMASTER_JEV_MODE=live` in `~/.claude/settings.json` env and the user environment (running panes picked it up without restart); hook deadline raised to 1500 ms after live latency measured 850–1220 ms (900 ms timed out every call); Hermes restarted (health 200, unauth 401, 51 tools).
- [x] S1 backlog revalidation run with its own cost cap (`jev-revalidate --backfill --tenant personal --max-usd`): 23k+ live decisions by 2026-09-24T01:35Z, 118 re-confirmed, 9 judged no longer useful, US$0.48; the steward continues at 500 per tenant per cycle.
- [ ] First 24 h: ≥95 % complete ledger rows, fallback < 20 %, cost < cap, zero unredacted egress, no surface stuck in breaker > 1 h.

**Found while running live (2026-09-23/24), fixed and reinstalled**
- [x] Hook deadline 900 ms → 1500 ms: live latency 850–1220 ms timed out every hook decision at 900 ms; 7/7 ok after.
- [x] Recall hook timeout 5 s → 10 s in settings: a recall hook killed at 5 s lost the whole recall block and left an orphan send intent.
- [x] S1/S4 per tenant (`7abaf1c`): the steward saw 403 of 41,368 stale claims; 99 % live in tenant `personal`.
- [x] S1 page cap 500 (`d14a46b`): a 41k backfill page exceeded SQLite's parameter limit and stopped S1.
- [x] Dreaming ledger column repair (`d09eb0c`): schema version 3 was recorded without `held_count`; the first 4.9.0 run extracted nothing, the next one applied 20 with S3 triaging 22.
- [x] Cut-emoji prompts sent instead of `request_invalid` (`8c8d57e`); in-flight intents are not orphans (`50f7a7f`); bounded ledger opens use the shared envelope (`fb9d776`).
- [x] Pushed as PR #254 (squash; secret-shaped test fixtures built from parts for push protection).
- [x] Python 3.10 keep-alive (`e913706`): 3.10's `http.client` leaves a response read to Content-Length open, so the next request on the pooled connection raised `ResponseNotReady` (CI ubuntu 3.10: 5 transport and 7 selector tests). The response is closed before pooling; reproduced and fixed on a local 3.10.
- [x] S1 backlog of tenant `personal` judged once (2026-09-24): 24,447 asked, 104 re-confirmed, 48 no longer useful, 24,295 kept stale, 122 sensitive skipped, US$0.48; it stopped on the 600 RPM cap, which `budget_exhausted` also reports.

**Phase 4 — re-test and independent review**
- [x] Full non-ML gate (incl. the disposable public demo) on the installed revision `7d536a8`, four sequential shards: 6676 passed, 0 failed, 74 skipped, 1 xfailed.
- [x] Live read-only checks 2026-09-24T01:24Z: operational review (database PASS, retrieval canary PASS, runtime pinned to 4.9.0 until main carries it, compiled_profile WARN while run 5 maps from claims, checkpoint WARN until the first Orca delivery, jev_decisions WARN: dedup/skills idle, 5 orphan intents traced to the memory-reaper kill and one killed recall hook); Hermes MCP health 200 / unauth 401 / 51 tools; ledger read through the dashboard tab and `jev-status`.
- [x] Independent adversarial review of the new code; no open high findings (high: `799dc1f`; medium/low: `80277a4`, `2af6f81`, `25bb5af`, `8e85b3b`).
- [ ] Reports at 24 h, 72 h and 7 days from dashboard numbers.

**Phase 5 — learning loop**
- [ ] Weekly per-question calibration (split-half, 95 % lower bound, ≥100 outcomes).
- [ ] Off-policy estimates (IPS/SNIPS/DR with n, ESS, clipping, interval) before any threshold change.
- [ ] 10-minute weekly operator review queue (actor `operator`).
- [ ] Local judge distilled once ≥20,000 item rows and ≥1,000 outcomes per surface; serves only if it matches Jev Brier on a time split.

**Phase 6 — profile and checkpoint**
- [ ] F-03 profile support younger than 7 days.
- [x] F-08 checkpoint poke retargeted to Orca (`~/.memorymaster/checkpoints/orca_poke.py`, WezTerm fallback); dry-run resolves the MemoryMaster Claude terminal.
- [ ] F-08 alert on failed delivery (operational review) and 7/7 days delivered.


### Semantic candidates and optional System One skills (2026-09-20)

Implemented in `cefed52` with the Windows stdio startup fix `f8146e6` on
`feat/semantic-candidates-jev-20260920` and installed
in both local-client and scheduled-worker Python environments on September 20.
Real-provider JEV usefulness remains unaccepted; the selector is disabled.

- Skill recall enumerates active authorized SQLite skills before ranking/limits;
  migration 25 provides a partial catalog index. Ordinary claims cannot crowd
  out skills. Returned IDs are rehydrated after selection.
- Hybrid recall admits a broader authorized pool (up to 2,000 embeddings),
  reserving a query-driven lexical stream. Keyset pages continue past rejected
  rows; scan work scales with the scoped corpus while candidate memory stays
  bounded. Planner `OR` expansion is deduplicated and capped at 32 subqueries.
- Optional TypeSafe JEV uses descriptions, then detailed shortlist verification,
  with explicit abstention and deterministic fallback on uncertainty, malformed
  output or timeout. SQLite owns lifecycle, scope, sensitivity and citations.
  The selector is off by default; see `docs/governed-skills.md` for limits.
- Verification: disposable candidate-starvation, authorization, retirement,
  real canceled-worker, migration/restore and facade integration regressions.
  The focused integration gate passed 145 tests; later transport/paging repairs
  passed their focused gates. Full non-ML run: 5,151 passed, 76 skipped,
  90 deselected, one expected failure and three failures in the retrieval-profile
  test double, which lacked the existing store pagination protocol. Only that
  double was repaired afterward; all five profile tests plus 23 isolated vector/
  embedding tests passed (28 total). The full suite was not rerun after this
  test-only repair. Ruff and generated release metadata checks passed.
  An earlier facade-size failure was fixed by placing candidate admission in
  `recall/candidate_pool.py`; architecture plus candidate/selector checks passed
  18 tests. Removing the lexical union intentionally reproduced the missing-hit
  failure; source was restored. Logs/XML remain in
  `artifacts/semantic-jev-20260920/`, including the failed full run.
- Independent read-only review (security reviewer, `gpt-5.6-sol`, high reasoning)
  found no remaining material blocker after timeout cancellation, malformed
  Choice rejection and authorized candidate paging repairs. Local tests and this
  review do not establish real-provider semantic efficacy.
- Deployment: both installed environments match all 414 wheel files, and the
  final 40-test installed gate passed with all 154 loaded MemoryMaster modules
  verified under site-packages. The scheduled Python passed the disposable
  public lifecycle demo. A consistent pre-migration backup passed integrity checks;
  additive migration 25 preserved all 148,323 claims and its catalog index is
  used. Live post-migration integrity and foreign-key checks passed. Detailed
  evidence is in the existing `LIVE-DEPLOYMENT.md` ledger and ignored
  `artifacts/semantic-jev-deployment-20260920/` receipts.
- Deployment verification exposed a Windows native-ML import stall after stdio
  startup. Importing optional SentenceTransformers before the reader starts
  fixes it without eager model construction. The regression failed before the
  fix; 21 startup/public-MCP/HTTP/architecture checks then passed. Missing/broken
  optional packages still permit startup, and other platforms remain lazy.
- Real-provider evaluation/activation is blocked by absent `TYPESAFE_API_KEY`.
  The live database also has zero active confirmed skills; tests establish the
  candidate correction, not a measured live skill-quality gain. Held-out
  Spanish/English positive and abstention cases must show improvement over
  deterministic fallback before activation. Installed filesystem skills are
  outside this selector's scope; approval remains human-controlled.

### Installed useful-selection correction (operator order 2026-09-08)

The 13-record review exposed an end-to-end failure: source-backed output was
mistaken for useful memory. This work **supersedes the earlier next step of
asking the operator to technically review emitted records** and the implication
that passing citation/format or delivery checks establishes useful selection.
Earlier deployed-state and test records below remain dated evidence, not current
quality acceptance. The single execution checklist is the existing
`.planning/audits/2026-09-07-e2e-review/IMPLEMENTATION.md`.

- [x] Freeze the 13 failure cases and independent useful positives; demonstrate
  the current write/promotion gaps before changing implementation.
- [x] Extend the existing source-aware Dreaming review with a versioned,
  mandatory usefulness/novelty decision, concrete future benefit, canonical
  destination and exact scope. Reject/unknown must not write an active candidate.
- [x] Compare relevant authorized memories and batch peers, preserve source
  independence, and reject transport/global misattribution without opening scope
  or tenant access. No extra agent, provider, database or scheduled process.
- [x] Bind Steward confirmation to the same accepted review. Old/missing or
  modified receipts cannot authorize a new confirmation. Keep existing historical
  rows intact; rejected evidence stays in the Dreaming ledger for diagnosis.
- [x] Exercise capture -> extraction -> consolidation -> candidate -> Steward ->
  cited recall -> retirement in disposable storage, including useful positives,
  duplicate paraphrases, uncertainty, replay, cross-scope and hostile inputs.
- [x] Run a bounded actual-provider comparison on frozen redacted inputs, separate
  model outcomes from fixture plumbing, and report precision/recall/cost with
  sample limits. Do not label AI judgments human or equate rejection of all with
  quality. Run regressions, full non-ML gate and candidate package smoke.
- [x] Deliver the tested candidate and update this checklist with exact evidence.
  The operator subsequently requested operational completion: both Python
  environments are installed, HTTP restarted, client/hook imports corrected,
  real Dreaming plus MCP/hook delivery verified, and all seven naturally scheduled
  operational-review checks PASS. Concurrent HTTP/stdio delivery also passed.
  Current evidence is in
  `.planning/audits/2026-09-07-e2e-review/LIVE-DEPLOYMENT.md`.
  Historical curation and human labels were not fabricated as deployment work.

Source implementation `fea3ff6` and bounded provider results are recorded in
`.planning/audits/2026-09-07-e2e-review/USEFUL-SELECTION.md`: final blinded selection
retained 9/9 useful cases and rejected 14/14 negatives; the separate original
13-record replay emitted none. These are development-set results, not a live
precision estimate. Final non-ML regression: 5,118 passed, 75 skipped, 90 deselected,
1 xfailed; native exit 0. The isolated wheel passes both useful/rejected lifecycle
checks and matches all 411 packaged source files. Candidate artifacts and their
manifest are in `artifacts/useful-selection-20260908/release`; installation and
historical curation have not been performed.

Agent-owned review covers truth, chronology, marginal usefulness and duplication.
Only preferences/business facts unavailable to technical investigation need the
operator. Existing human-provenance metrics remain honest; this task does not
manufacture human labels or lower the existing acceptance thresholds.

### Deployed: useful memory, reliable delivery (4.8.9)

PRs #252 and #253 merged and local runtimes upgraded on 2026-09-06 UTC. HTTP
MCP and installed hooks verified. Clients predating 4.8.8 require reconnect;
4.8.9 adds installer corrections and does not require restarting 4.8.8 clients.
See `.planning/USEFUL-MEMORY-V1.md` for exact evidence and limits.

Operational activation approved September 6: the operator requested enabling
the reviewed Dreaming, governed graph-observation and compiled-profile lanes.
User-level graph/profile settings now match the enabled scheduled launcher;
Dreaming retains candidate application, Gemini-only selection and existing
budgets. This resolves the dated shadow/off discrepancy below prospectively,
not retroactively. Steward approval and opt-in observation recall remain
mandatory. Weekly profile cadence and resumable per-cycle limits remain intact.
Workflow promotion, wiki generation, retired tasks and historical cleanup are
not included in this activation. Real-run validation is recorded in the ledger;
enabled does not mean semantic quality has been established.

Operator-approved on 2026-09-05 after refreshing seventeen upstream repositories.
Implement native improvements, not another framework or always-running agent:

- Preserve automated-event filtering and session briefing deduplication through
  hook installation, including compaction reset and customized-hook protection.
- Replace the misleading optional CI evaluation with the existing tracked qrels
  and governed lifecycle checks, retaining real failure and artifact evidence.
- Extend the existing Dreaming evaluator to report missed useful facts, unwanted
  emissions and explicit human-review provenance. Sample source captures
  read-only, including captures with zero candidates.
- Do not change ranking, graph membership or Dreaming thresholds on the basis of
  upstream marketing or zero-output counts. Ship a scorer/rescue experiment only
  after matched source labels demonstrate the actual gap.

Implementation and verification: [bounded ledger](.planning/USEFUL-MEMORY-V1.md).
No new database, process, scheduler, provider, historical curation or activation
change is part of this milestone. Gemini remains the selected deployment provider.

### Prior deployed state

The September 5 integration progressed from 4.8.6 to deployed 4.8.7;
the dated deployment ledgers below preserve what was actually verified.
PPR-7 graph observations and the compiled profile shipped in 4.7.
Workflow Intelligence is integrated on main (#248); its hooks and promotion
remain separately gated. Integrated code does not imply activation.

- The live read-only operational artifact at 2026-09-05T09:55:40Z reports
  installed 4.8.5, migration 24, database integrity passing, zero recent
  private-context matches and the retrieval canary at rank 4.
- That artifact reads graph/profile flags from its own process and reports
  disabled. Follow-up inspection proved the scheduled launcher overrides both
  flags to enabled. Its PASS therefore does not establish worker activation or
  live synthesis precision. Recent worker logs show discovery with no components.
  Existing profile state contains 52 active facts and 565 exact supports,
  with zero manifest mismatches in that run.
- The documented August 21 operator intent is SHADOW. September 5 live
  inspection instead found `--apply-candidates` and recent `dry_run=0`
  runs. This is an unresolved activation discrepancy, not shadow proof.
  Current consolidation uses Gemini through Antigravity; the earlier
  Gemini/GLM pairing is historical. This pass did not change worker mode.
- PPR-7 and profile contracts live in their bounded implementation ledgers.
  Use effective runtime configuration and provider-call evidence for current
  models, limits, generation status and injection; do not reuse August task
  prompts as current policy.
- Local 2026-09-05 improvements and their verification are recorded in
  [.planning/ENGINEERING-CONSOLIDATION-2026-09-05.md](.planning/ENGINEERING-CONSOLIDATION-2026-09-05.md).
  This is an implementation record, not a second roadmap.
- The operator now authorizes integration/deployment and removal of GLM.
  Gemini remains the active extraction/consolidation/profile provider.
  Evaluate existing Dreaming history before generating more data; exact
  citations, steward confirmation and access counts are not semantic precision.

## Next

- Scoped consolidation and Gemini retirement are integrated; evaluate existing
  Dreaming outputs for citation entailment before generating more data.
- For future acceptance, measure user benefit on capture -> cited recall ->
  retirement using the installed package and actual client. Live precision,
  cost and retrieval impact need real eligible samples; empty or disabled
  queues do not earn quality scores.
- Keep workflow hooks, generation activation, live data curation and private
  runtime upgrades under their existing explicit operator decisions.
- Preserve historical P5 failures and repairs in the dated audit ledgers;
  those elapsed windows are not new implementation prerequisites.

### Research-derived memory sustainability program

This program turns useful research into reproducible MemoryMaster experiments;
papers are prior art, never trusted memory or implementation authority. The
first reviewed input is `arXiv:2607.26637`, *Filesystem-Based Memory for LLM
Agents: Organization, Evolution, and Sustainability*. Its useful lesson is to
measure preservation, answer quality, and total retrieval cost separately:
organization may reduce search cost without improving answers, and uncontrolled
rewriting can erase temporal, emotional, or narrative information.

- **R0 - Governed paper radar:** build a deterministic, read-only metadata
  importer for all sections of
  `VoltAgent/awesome-ai-agent-papers`, initially pinned at upstream commit
  `c8502b6acd3978a84b8b25453eda24be83088d00`. Record source revision, observed
  time, section, title, canonical arXiv ID/version, links, and upstream summary;
  deduplicate by canonical arXiv ID and DOI. Snapshot and diff additions,
  removals, retitles, duplicate IDs, broken links, and displayed-count drift.
  A refresh may update only the non-authoritative research ledger; it must not
  mutate runtime configuration, the roadmap, evidence, claims, or skills.
- **R0 review funnel:** ingest metadata for the full list without downloading
  every PDF. Score entries against governance/lifecycle, retrieval/routing,
  procedural skills, graph/evidence, evaluation/cost, reliability, privacy, and
  security. Fetch primary arXiv metadata for shortlisted items and full text
  only for a bounded review batch. Every reviewed paper receives an explicit
  `adopt`, `benchmark`, `defer`, or `reject` verdict with primary citations,
  reproducibility notes, expected benefit, implementation surface, and cost.
  The 2026-08-08 checkpoint parsed all 57 Memory & RAG records and completed
  primary-PDF result/limitation review for an 18-paper priority batch, including
  the earlier filesystem-memory paper and cross-section active-use/cost work.
  Decisions and the ordered PPR-1 through PPR-6 packages are recorded in
  `.planning/PAPER-RADAR-REVIEW-2026-08-08.md`; importer and runtime experiments
  remain unimplemented.
- **R1 - Representation-preservation benchmark:** add private, synthetic, and
  publishable fixtures covering latest-versus-superseded state, affect and
  emphasis, narrative-arc co-retrieval, ordinary factual recall, and procedural
  reuse. Score answer correctness separately from citation correctness and raw
  evidence preservation so a cited but temporally wrong answer cannot pass.
  The offline PPR-1 checkpoint now provides eight publishable synthetic cases,
  a five-profile prediction contract, independent answer/citation/tool scores,
  exact parameter-provenance checks, and deterministic failure attribution.
  Product-profile baselines and behavior changes remain later gated work.
- **R2 - Explicit consumer-aware recall projections:** compare governed claims
  plus bounded evidence for strong consumers, concise task guidance for smaller
  consumers, lifecycle timelines for temporal/high-stakes questions, and
  confirmed skills plus warnings for procedural tasks. The caller selects a
  versioned profile; MemoryMaster must not infer a weaker trust mode or silently
  change lifecycle and scope rules. The offline PPR-3 checkpoint now defines
  explicit low/balanced/high/temporal/procedural policies and deterministic
  admission diagnostics. It remains a content-free shadow evaluator and does
  not alter the production retrieval plan.
- **R3 - Ephemeral guidance and outcome-aware skills:** synthesize cited,
  token-budgeted task guidance from confirmed authorized skills without storing
  or promoting the synthesis. Extend skill evidence with
  `success`/`failure`/`ambiguous` outcomes; failed traces may generate warnings
  but cannot reinforce a positive procedure. Promotion remains human-only. The
  offline PPR-6 checkpoint now validates content-free execution observations,
  consumer/model and tool-schema snapshots, activation/termination/validation
  results, bounded metrics, deduplication, and separate negative warnings.
  Durable outcome persistence and runtime review wiring remain gated.
- **R4 - Prioritized paper experiments:** start with query-budget routing
  (`BudgetMem`), progressive evidence sufficiency and source rehydration
  (`A2RAG`), generator-aligned evidence pruning (`Less is More for RAG`),
  intent-aware retrieval, temporal occurrence-time modeling, deterministic
  versus LLM graph extraction, and action-oriented memory evaluation
  (`Mem2ActBench`). Adopt none until a focused baseline/mutation comparison
  proves a gain on an authoritative MemoryMaster execution path. The offline
  PPR-4 checkpoint now provides bounded claim-to-evidence rehydration with
  active-source and scope/sensitivity revalidation; it is explicit evaluation
  functionality and is not a new default answer path.
- **R5 - Governed temporal projection:** the offline PPR-5 checkpoint now adds
  explicit current, latest, historical, and occurrence-time projections;
  inclusive interval overlap; citation-complete structural durative summaries;
  and bounded episode windows derived only from authorized linked evidence and
  stable source/session metadata. These rebuildable projections do not change
  schema, production ranking, or default recall.
- **R5 - Sustainability and cost gates:** measure current-versus-superseded
  errors, early-memory survival, duplication/fragmentation, citation accuracy,
  tokens, content read, tool/provider calls, latency, and cost per correct answer
  or solved task. Compare claims-only, evidence-only, claims+evidence,
  claims+approved-skills, and claims+ephemeral-guidance profiles with both a
  smaller OAuth-backed model and the stronger OAuth-backed judge. The offline
  PPR-2 checkpoint now provides a bounded aggregate-safe stage schema and a
  disposable-SQLite observer over authoritative retrieval and packing. It does
  not persist query/evidence text or enable any provider/model by itself.

Exit gates: zero secret or cross-scope leakage, zero automatic promotion,
replay-safe radar updates, primary-source traceability for every verdict, no
LongMemEval or full-QA regression beyond existing thresholds, and a measured
quality or cost win before any experimental retrieval behavior becomes a
default. Offline synthetic harness work may proceed under explicit operator
authorization; runtime experiments and activation begin only after the clean P5
observation/PR gate closes.

## Later

- Continue the measured service-facade decomposition without breaking the
  compatibility surface.
- Revisit shared multi-user/team operation only if a real use case appears;
  its Postgres, RLS, identity, deployment, and recovery gates remain deferred.
- Revisit authenticated Qdrant, immutable container images, and Kubernetes/Helm
  only for an explicitly selected semantic or hosted profile.
- Improve entity aliases and steward classification only against versioned,
  reproducible evaluation datasets.
- Expand companion integrations through the documented provider protocols and
  core-to-companion import boundary.
- Revisit hosted cloud, broad backend matrices, shared multi-user operation,
  extra SDK languages, graph-answer agents, community detection, and a full
  ontology editor only after a separately approved use case.

## Not planned

- Automatic live cleanup, compaction, redaction, migration, archival, retention
  deletion, or backlog mutation without an explicit operator action.
- Synthetic production evidence, silent provider fallbacks, or direct Qdrant
  truth that bypasses authoritative rehydration and governance.
- A second authoritative roadmap, another default vector database, or a
  flag-day rewrite of `MemoryService`.
- Making Postgres, Qdrant, containers, or multi-user operation a dependency of
  the personal/local minimal profile.
- Adding Cognee as a runtime dependency or replacing governed claims with
  graph/vector output.
- Bulk-importing paper full text into governed memory, treating curated-list
  metadata as verified evidence, or automatically implementing research claims.
- Replacing SQLite claim authority with a filesystem hierarchy, restoring the
  Obsidian projection as the read layer, or allowing an LLM to rewrite, merge,
  compact, or delete authoritative history autonomously.
