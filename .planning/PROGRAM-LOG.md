# MM v4 Consolidation Program — Execution Log

Contract: `C:\Users\pauol\artifacts\2026-06-09-mm4-consolidation-plan.html` · claim mm-dc73 · baseline mm-7e89
Mode: **autonomous e2e** (user delegated all phase gates 2026-06-10: "i dont want to babysit no more, workflow the whole plan e2e").
Operator = Claude session drives phase workflows sequentially, verifies exit gates with real commands (never agent self-reports),
makes reserved decisions with conservative defaults, logs everything here. Invariant: main stays releasable after every phase.
Boundaries that survive autonomy: sensitivity filter untouched, no secret handling, full-suite + ruff gate before every merge,
conventional PRs, numeric exit gates only.

## Status

| Phase | State | Evidence |
|---|---|---|
| pre: ship v3.28.0 | ✅ DONE 2026-06-09 | PyPI 200; PRs #146 #147 |
| CI resurrection (unplanned) | ✅ DONE 2026-06-09 | PRs #148 #150; main CI green (run 27244754336) — first since June 1 |
| P0 /mm4-baseline | ✅ DONE 2026-06-09 | gate 4/4; BASELINE-2026-06-09.html; PR #149; graphify hook fixed |
| P1 /mm4-reliability | ✅ DONE 2026-06-10 | PR #151 + #152. EXIT GATE PASSED: chaos soak 46min/12 writers/20 kill-rounds × both flag modes = 0 quick_check fails, 0 FK orphans, 0 lost acked writes. Live DB: FK 401→0 (400 quarantined), WAL 1.44GB→0 (first checkpoint in project history), cold init 16.06s→1.15s plain/0.09s fastpath. Flags enabled user-wide for dogfood (rollback = delete env vars). Daemon escalation tripwire stands |
| P2 /mm4-restructure | ✅ MOVES DONE 2026-06-15 (merge pending) | All 8 batches committed (4d40667 phase0 · 6620598 bridges · 86ed4f2 surfaces · 7dee6db knowledge · e55bfaa recall · 5b6b8dd govern · 1bcc111 stores · 5f1db7a core) + llm_budget cycle-tidy. 138 flat modules → 7 subpackages (core/stores/recall/govern/knowledge/surfaces/bridges) + 3 shim-pkgs; **0 real flat modules** (108 top-level .py all compat shims); skill_evolver deleted; 5 orphans deprecation-marked; full suite 2830 green every batch; ruff clean; all 5 entry points boot. RESIDUAL (documented, suite-proven-harmless): subpackage graph is a DAG except `core→govern`/`core→recall` via **service.py** (the orchestrator facade legitimately sits atop all layers but census placed it in core). Phase0 module-level SCC pins hold; no ImportError. Strict-DAG via relocating service.py to a facade tier = P5 follow-up. |
| P3 /mm4-quality | pending | |
| P4 /mm4-agents | pending | |
| P5 /mm4-surfaces | pending | |
| P6 /mm4-release (v4.0.0) | pending | |

## Reserved-decision defaults (user-delegated)

- P1 architecture: judge panel decides; going-in recommendation write-broker (smallest migration). Rollout behind env flag, legacy path preserved.
- P2 kill/keep: NOTHING deleted that has any usage evidence; prefer deprecate/merge over delete; every verdict logged here for retroactive review.
- P2 cycle-cleanliness: relocate genuine misplacements (llm_budget→core, done); do NOT do risky lazy-import surgery on service.py to chase a strict subpackage DAG — accept orchestrator-facade coupling, defer service.py relocation to P5.
- P6 publish: v4.0.0 to PyPI authorized by standing full-autonomy feedback (memory: 'hace TODO' covers PyPI publish).

## Phase log

### 2026-06-09 — pre-program + P0 + CI (see Status). Key numbers frozen in BASELINE-2026-06-09.html.
### 2026-06-10 — P1 WF1 (design judge panel) DONE (run wf_c0e425c6-315, 7 agents, 719k tokens).
Verdict: 3-way vote tie; scores daemon/minimal tied 21.5, broker 16 — tie broken to lower migration risk → **minimal (WAL-Discipline)**.
Root causes PROVEN by the panel (each with a targeted kill in the spec):
1. scripts/openclaw-sync.sh scp-uploads OVER the live DB while ~12 writers hold it open — most plausible 06-05 corruption mechanism (corruption was confined to idx_verbatim_session).
2. verbatim_store._connect (the hottest write path — Stop hook, the table that corrupted) sets NO busy_timeout.
3. WAL = 1.44GB, zero wal_checkpoint calls exist anywhere — explains the 16.06s cold init (350k-frame replay).
4. ~20 ad-hoc sqlite3.connect sites with divergent pragmas; read path WRITES (_record_accesses UPDATE on every recall).
Grafts kept: env-flag gating + bypass observability (broker), RO-recall + access spool (daemon finding), daemon escalation as falsification tripwire.
WF2 = 12 sequential build steps on branch omni/p1-reliability; chaos soak (step 12) is the P1 exit gate.
