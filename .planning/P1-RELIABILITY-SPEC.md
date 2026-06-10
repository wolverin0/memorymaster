# P1 Reliability Build Spec — WAL-Discipline + Scheduled Integrity (v3.29 target)

**Status**: APPROVED (design-panel synthesis, 2026-06-10)
**Verdict**: `minimal` (WAL-Discipline hardening) — chosen by judge tally, with grafts from the broker and daemon drafts.
**Tally**: verdict votes 1/1/1 (broker/daemon/minimal — tie) → score totals broker 16.0, daemon 21.5, minimal 21.5 (broker eliminated; daemon/minimal tied) → tie broken toward lower migration risk: the migration-risk judge scored minimal 7 vs daemon 6, and minimal is the only design with zero new resident processes, zero spawn-mechanism changes, and no flag-day. **Winner: minimal.**
**Escalation clause**: if btree corruption recurs after this spec is fully deployed, the WAL-discipline hypothesis is falsified and P1.5 escalates to the **daemon** design (whole-file process ownership — the runner-up that two judges agreed is the only architecture that fully *retires* the corruption class). The metrics in §7 are the tripwire.

---

## 0. Problem statement (evidence-grounded)

~12 concurrent writer processes share one 3.47 GB SQLite file. Real index-only btree corruption occurred 2026-06-05 (`memorymaster.db.corrupt-2026-06-05`, salvage in `scripts/recover_db_indexcorrupt.py`). v3.27 shipped busy_timeout + WAL hardening but writer count is unchanged.

Verified current-state facts (all re-checked against the working tree on 2026-06-09/10):

| # | Fact | Evidence |
|---|------|----------|
| F1 | Every `SQLiteStore.connect()` opens a brand-new connection, re-applies WAL + `busy_timeout=5000`, no reuse | `storage.py:37-51`; lost-write race documented in-code at `storage.py:43-47` |
| F2 | `init_db` runs `executescript` + 14 `_ensure_*` passes + `MigrationRunner` on every cold start | `storage.py:52-103` (MigrationRunner at `storage.py:100-103`); measured 16.06 s cold vs 0.091 s warm (`.planning/BASELINE-2026-06-09.html`) |
| F3 | **Zero** checkpoint/integrity discipline anywhere in the package: no `wal_checkpoint`, `quick_check`, `integrity_check`, or `VACUUM` | grep of `memorymaster/` — 0 hits (re-verified) |
| F4 | Live WAL is **1,443,256,632 bytes** against a 3,475,644,416-byte DB — passive auto-checkpoint is permanently starved by ~12 reader/writer processes | `ls -la memorymaster.db*` 2026-06-09 22:10 |
| F5 | `verbatim_store._connect()` sets WAL but **no busy_timeout**; its own comment calls it "the hottest write path"; the 2026-06-05 corruption was confined to `idx_verbatim_session` on this exact table | `verbatim_store.py:62-66`, comment at `:79-81`; `scripts/recover_db_indexcorrupt.py:3-5` |
| F6 | Legacy VM cron `openclaw-sync.sh` scp-uploads a merged copy **over the live DB** while writers hold it open — prime corruption mechanism | `scripts/openclaw-sync.sh:55-60` (cron line documented at `:10-11`) |
| F7 | ~55 raw `sqlite3.connect` sites across ~30 modules bypass `SQLiteStore.connect()` pragma discipline, with divergent settings (timeout=30 in `db_merge.py:288,346`; none in `feedback.py:27`, `entity_graph.py:105`, `query_cache.py:74`, `rule_miner.py:108`, `daily_notes.py:37,128`, `claim_edges.py:141,235`, `dream_bridge.py:415`, `session_tracker.py:38`, `wiki_engine.py:69,368,615,798,861`, `llm_steward.py:634`, …) | grep `sqlite3\.connect` over `memorymaster/` |
| F8 | The installed steward hook does a **raw UPDATE with no busy_timeout and no WAL pragma** | `config_templates/hooks/memorymaster-steward-cycle.py:51-57` |
| F9 | **The read path writes.** The recall hook is a per-prompt writer: `context_hook.py` builds a `MemoryService` and calls `query_for_context` (~`context_hook.py:1259`), which hits `service.py:769/843/913 → _record_accesses (service.py:1034-1050) → record_accesses_batch` = `UPDATE claims SET access_count=… + commit` (`_storage_lifecycle.py:512-527`), plus `FeedbackTracker.record_retrieval` writes (`service.py:1052-1062`). All wrapped in `contextlib.suppress(Exception)` — a naive read-only connection would **silently** kill tiering/decay signal. Side lookups also open RW at `context_hook.py:643` and `:914`. |
| F10 | **401 orphan FK rows on the live DB right now** (recovery collateral): `PRAGMA foreign_key_check` → events→claims 226, citations→claims 159, claim_links→claims 6, claim_embeddings→claims 6, claims→claims (self-FK) 4. Verified read-only 2026-06-10, 2.1 s. |
| F11 | Qdrant sync is fire-and-forget with no reconciliation: `_qdrant_sync` swallows exceptions (`service.py:323-334`), `_qdrant_post_cycle_sync` (`service.py:335`) only re-upserts post-cycle; `QdrantBackend.sync_all` exists but is manual (`qdrant_backend.py:270`) | `.planning/codebase/CONCERNS.md` #3 |
| F12 | Snapshot machinery already exists using the SQLite backup API (`snapshot.py:73-98 create_snapshot`, `:126-143 backup`, `:202 list_snapshots`) — extend, don't reinvent | `memorymaster/snapshot.py` |
| F13 | `run_cycle` is the natural wiring seam for new scheduled phases: jobs pattern `jobs/<name>.run(self.store, …) → result dict`, cycle ends at `self._qdrant_post_cycle_sync()` | `service.py:520-606` |
| F14 | Connection-open retries: 3 attempts, 0.5/1/2 s backoff | `retry.py:28-36, 39-79` |
| F15 | Per-pane MCP servers build a fresh `MemoryService` per tool call; DB path overridable via `MEMORYMASTER_DEFAULT_DB` | `mcp_server.py:290-291`; env default resolution near `mcp_server.py:270-277` |
| F16 | OneDrive is exonerated as a *live* threat (OneDrive.exe not running, no configured account — per draft-3 verification), but the DB still lives under `G:\_OneDrive\...` and must eventually move | follow-up, §9 |

## 1. Architecture decision

**Keep the multi-process topology. Fix the operating envelope.** No broker, no daemon, no new resident process in P1:

- All processes keep opening the DB directly; connection opening is **centralized** into one helper with uniform pragmas (kills F7/F8).
- The recall hook becomes a **read-only** DB client; its access/feedback writes are **spooled** (append-only JSONL) and drained by the steward — this is the graft that fixes the silent-regression both judges flagged in F9.
- Ambient high-frequency writers (Stop-hook verbatim/learnings, dream bridge) also write to the spool instead of opening the 3.47 GB DB per event.
- Integrity becomes a **scheduled steward phase**: `wal_checkpoint(TRUNCATE)` every cycle, `quick_check` daily, `VACUUM INTO` snapshot weekly (F3/F4).
- Root-cause kills ship day 1: guard the legacy scp-over-live-DB path (F6), busy_timeout in verbatim_store (F5).
- The 401-orphan-FK repair (F10) and the Qdrant reconciliation job (F11) ship in P1 unconditionally.
- **Grafted from broker**: everything behavior-changing is gated behind an env flag with the untouched v3.27 path as the else-branch, plus bypass/health counters for observability (the broker draft's best discipline; the minimal draft's flaglessness was its judged weakness).
- **Grafted from daemon**: the explicit falsification tripwire + escalation path to process-ownership (§0, §7), and spool-replay idempotency reasoning (safe because ingest dedupes on idempotency_key/content-hash, `service.py:386-405`, and `record_accesses_batch` is monotonic-increment only).

**Writer-count effect**: recall hook (every prompt), Stop hook, and dream bridge leave the writer set entirely → remaining writers are the per-pane MCP servers (interactive ingest only), the 6 h steward, and the 15-min hermes delta sync. Repair swaps stay annoying (panes still hold handles) — that residual is accepted in P1 and is exactly what the escalation clause covers.

## 2. Components

### 2.1 `open_conn()` / `connect_ro()` — `memorymaster/_storage_shared.py` (+~70 LOC)
Single place that opens SQLite connections:
- `open_conn(db_path, *, busy_ms=15000)`: `row_factory=Row`, `foreign_keys=ON`, `journal_mode=WAL`, `busy_timeout=15000` (up from the divergent 0/5000/30000 sites), wrapped in `connect_with_retry` (`retry.py:39`).
- `connect_ro(db_path, *, query_ms=2000)`: `file:…?mode=ro` URI + `query_only=ON` — the connection **cannot** take a write lock (pattern already proven in-tree: `recall_tokenizer.py:192,229`, `verbatim_recall.py:184`, `memorymaster-session-start.py:237`).
- `SQLiteStore` gains `connect_ro()` delegating to it; `SQLiteStore.connect()` (`storage.py:37-51`) delegates to `open_conn` (behavior-identical except busy_timeout 5000→15000, which is strictly safer).

### 2.2 Read-only recall + access-record spool — `memorymaster/spool.py` (new, ~150 LOC), `context_hook.py`, `service.py`
- Flag `MEMORYMASTER_WAL_DISCIPLINE=1` puts the recall hook's `MemoryService` store into RO mode (store constructed with `read_only=True`; all read paths use `connect_ro`; side lookups at `context_hook.py:643/:914` follow automatically through the store).
- `_record_accesses` (`service.py:1034`): when the store is RO, instead of the suppressed UPDATE, append one JSONL line to the spool: `{"op":"access","claim_ids":[…],"ts":…,"query_hash":…}` and `{"op":"feedback",…}`. **No signal is lost** — this is the explicit fix for the connect_ro silent-no-op defect.
- Spool location: `~/.memorymaster/spool/<db-name>/` — **outside** the OneDrive-synced tree and outside the DB directory. Append-only, one file per writer-process per day (`{pid}-{date}.jsonl`), `O_APPEND` writes ≤4 KB (atomic on NTFS for practical purposes); drainer renames file before reading so writers never race the reader.
- This JSONL line format is the only "wire protocol" in the design. Envelope: `{"v":1,"op":"access"|"feedback"|"ingest"|"verbatim"|"dream","ts":<iso8601>,"idempotency_key":<str|null>,"payload":{…}}`. Unknown `op`/`v` → line preserved in a `quarantine/` subfolder, never dropped silently.

### 2.3 Ambient-write spool — `config_templates/hooks/memorymaster-auto-ingest.py`, `memorymaster-dream-sync.py`
Stop-hook learnings + verbatim turns and dream-bridge items append `op:"ingest"`/`op:"verbatim"`/`op:"dream"` lines instead of opening the DB. Behind the same flag; the current direct-write code remains the else-branch. Latency win: the hook drops from a 3.47 GB DB open + insert to a ~10 ms file append.

### 2.4 Spool drainer — `memorymaster/jobs/spool_drain.py` (new, ~150 LOC)
Steward-cycle phase (and one-shot CLI `drain-spool`): renames spool files, replays lines through the **normal paths** — `svc.ingest` (sensitivity filter preserved, per `.claude/rules/sensitivity-filter.md`: every new ingest path is default-deny until the filter is wired — it is, because we reuse `svc.ingest`), `store_verbatim`, `record_accesses_batch`, `FeedbackTracker`. Idempotent on replay (`service.py:386-405` content-hash + idempotency_key dedup; access counts are monotonic increments where a rare double-count is harmless). Reports `{drained, quarantined, lag_seconds}` into the cycle result.

### 2.5 Integrity steward phase — `memorymaster/jobs/integrity.py` (new, ~180 LOC)
Wired at the end of `service.run_cycle` (after `_qdrant_post_cycle_sync()`, `service.py:604`), ships **default-on** (additive, read-only or standard pragmas):
1. **Checkpoint** (every cycle): `PRAGMA wal_checkpoint(TRUNCATE)` on a dedicated connection with `busy_timeout=30000`; log `(busy, log_frames, checkpointed_frames)` + resulting `-wal` size. If WAL > 256 MB after the attempt, emit `integrity_wal_oversize` event (tripwire input).
2. **quick_check** (throttled to 1/day via a stamp in the existing meta/kv table): `PRAGMA quick_check` on a `connect_ro` connection. On any non-`ok` row: write sentinel file `<db>.integrity-failed`, emit `integrity_check_failed` event, **freeze steward promotions** (validator/deterministic phases check the sentinel and no-op), alert via the existing operator-alert path. Never auto-destructive.
3. **foreign_key_check** (1/day, read-only): report orphan count as a metric; non-zero after the Step-5 repair = regression alert.
4. **VACUUM INTO snapshot** (1/week): `VACUUM INTO 'snapshots/mm-YYYYMMDD.db'` via a new `snapshot.vacuum_into()` alongside the existing backup-API machinery (`snapshot.py:73-143`); keep 3 rotations; snapshot dir configurable via `MEMORYMASTER_SNAPSHOT_DIR` (default `~/.memorymaster/snapshots/`, again outside the synced tree). Replaces the ad-hoc 3.6 GB `.bak` copies.

Also: one **supervised manual TRUNCATE** at rollout (operator runbook step) to retire the current 1.44 GB WAL, and a checkpoint piggyback added to `scripts/windows-hermes-sync.ps1`.

### 2.6 Orphan-FK repair — `memorymaster/jobs/fk_repair.py` (new, ~140 LOC) + CLI `repair-fk`
Repairs the 401 verified orphans (F10). Dry-run by default; `--apply` does, in ONE transaction:
1. `PRAGMA foreign_key_check` → group by (table, parent).
2. Export every orphan row verbatim to `~/.memorymaster/quarantine/fk-repair-<ts>.jsonl` (audit trail, restorable).
3. Dispose: `events`/`citations`/`claim_links`/`claim_embeddings` orphans → DELETE (children of lost claims, meaningless without parents); `claims.claims` self-FK orphans (4 rows: dangling `supersedes_claim_id`/`replaced_by_claim_id`/`entity` refs) → NULL the dangling pointer, keep the claim, emit a `fk_repair` event per touched claim (status transitions stay within `claims-lifecycle.md` rules — no status edits via SQL; only the dangling FK column is nulled).
4. Re-run `foreign_key_check`, assert 0, print before/after.
Idempotent (second run is a no-op). Run ONCE supervised on the live DB after merge; thereafter the daily integrity phase (§2.5.3) only detects.

### 2.7 Qdrant reconciliation — `memorymaster/jobs/qdrant_reconcile.py` (new, ~120 LOC)
Steward-cycle phase (skipped when `QDRANT_URL` unset or backend unavailable, mirroring `service.py:305-321`), throttled to 1/day:
1. Drift metric: SQLite truth count (active non-archived claims with embeddings eligibility) vs Qdrant point count; emit `qdrant_drift` event with both numbers.
2. If |drift| > threshold (`MEMORYMASTER_QDRANT_DRIFT_MAX`, default 100) or `--full`: run `QdrantBackend.sync_all(store)` (`qdrant_backend.py:270`) in batches; delete points whose claim is archived/missing; upsert missing.
3. Report `{sqlite_count, qdrant_count, upserted, deleted}` into the cycle result + dashboard.

### 2.8 Day-1 root-cause kills (no flag — these are bug fixes)
- `verbatim_store.py:62-66`: route `_connect` through `open_conn` (gains busy_timeout=15000 on the table that actually corrupted).
- `scripts/openclaw-sync.sh`: prepend a hard guard (`echo "RETIRED 2026-06-10: scp-over-live-DB corrupts; use hermes delta sync"; exit 1`) so a forgotten VM cron can never again upload over the live file (F6). Operator action: audit the Hermes VM crontab + `/var/log/memorymaster-sync.log` for activity on 2026-06-05 (evidence item for the incident postmortem).
- Steward hook template (`config_templates/hooks/memorymaster-steward-cycle.py:51-57`): replace raw connect with `open_conn` (or better, move the auto-archive UPDATE behind a service helper).

### 2.9 init_db fast-path — `storage.py` (+~25 LOC), flag-gated
Stamp `PRAGMA user_version` with a schema fingerprint after a successful full `init_db`; when the stamp matches, skip the 14 `_ensure_*` passes + MigrationRunner probe (F2). Behind `MEMORYMASTER_INITDB_FASTPATH=1` (sub-flag of WAL_DISCIPLINE), because skipping `_ensure_*` on a lagging DB is the one genuinely risky optimization here. Target: cold init 16.06 s → <2 s (baseline target); must be **re-measured**, not assumed.

### 2.10 Observability — `memorymaster/jobs/integrity.py` + dashboard
Per-cycle metrics persisted via `store.record_event` and surfaced in `dashboard.py`: WAL bytes, checkpoint result, quick_check status, fk orphan count, qdrant drift, spool depth + drain lag, busy-error count (counter incremented in `open_conn`'s retry wrapper). These are the §7 flip criteria AND the escalation tripwire.

## 3. Ordered build steps

All steps run by a single builder agent, **sequentially, in the MAIN checkout** — the package is editable-installed, so pytest inside a worktree silently imports the main checkout's code (import pin) and lies; do not use worktrees for these steps. Each step is independently testable and ends with `python -m pytest tests/ -q --tb=short` + `ruff check memorymaster/` green.

1. **Day-1 kills + guards.** Files: `scripts/openclaw-sync.sh` (exit-1 guard), `memorymaster/verbatim_store.py` (busy_timeout via local pragma, pre-`open_conn`), `scripts/RUNBOOK-wal-truncate.md` (new: supervised TRUNCATE procedure + VM crontab audit checklist). Test: `tests/test_verbatim_store_pragmas.py` (new) asserts busy_timeout set on `_connect`.
2. **Connection helpers.** Files: `memorymaster/_storage_shared.py` (`open_conn`/`connect_ro`), `memorymaster/storage.py` (`connect()` delegates; add `connect_ro()`). Test: `tests/test_open_conn.py` (new) — pragma assertions (WAL, busy 15000, foreign_keys, ro-mode write attempt raises `OperationalError`), retry behavior preserved.
3. **Ad-hoc site migration.** Files: `verbatim_store.py`, `llm_steward.py:634`, `db_merge.py:288,346`, `feedback.py:27`, `entity_graph.py:105`, `query_cache.py:74`, `rule_miner.py:108`, `daily_notes.py:37,128`, `claim_edges.py:141,235`, `dream_bridge.py:415`, `session_tracker.py:38`, `wiki_engine.py:69,368,615,798,861`, `contradiction_probe.py:84`, `transcript_miner.py:75`, `config_templates/hooks/memorymaster-steward-cycle.py:51` (one mechanical commit per 4-6 files; pure-reader sites move to `connect_ro`). Test: extend `tests/test_open_conn.py` with an AST/grep sweep asserting no remaining bare `sqlite3.connect(` outside `_storage_shared.py`, `snapshot.py` (backup API needs raw), and `mode=ro` URI sites.
4. **Integrity steward phase.** Files: `memorymaster/jobs/integrity.py` (new), `memorymaster/service.py` (wire after `service.py:604`), `memorymaster/snapshot.py` (`vacuum_into()` + rotation), `memorymaster/cli_handlers_basic.py` (`integrity` subcommand: `--checkpoint|--quick-check|--vacuum-snapshot|--status`), `scripts/windows-hermes-sync.ps1` (checkpoint piggyback). Tests: `tests/test_integrity_job.py` (new) — checkpoint on a temp DB with synthetic WAL, quick_check ok + injected-corruption sentinel path (corrupt a copy with a hex edit), promotion freeze on sentinel, vacuum-into rotation keeps 3, daily/weekly throttles.
5. **FK repair.** Files: `memorymaster/jobs/fk_repair.py` (new), `memorymaster/cli_handlers_basic.py` (`repair-fk`). Tests: `tests/test_fk_repair.py` (new) — seed a temp DB with orphans in all 5 observed shapes (events/citations/claim_links/claim_embeddings/claims-self), dry-run reports 401-style grouping without mutating, apply quarantines+repairs to 0, second apply is a no-op. **Operator step after merge**: run `repair-fk --apply` once on the live DB; record before(401)/after(0) in `PROGRAM-LOG.md`.
6. **Qdrant reconciliation.** Files: `memorymaster/jobs/qdrant_reconcile.py` (new), `memorymaster/service.py` (wire as throttled cycle phase), `memorymaster/cli_handlers_basic.py` (`qdrant-reconcile`). Tests: `tests/test_qdrant_reconcile.py` (new) — fake QdrantBackend; drift computed, threshold respected, sync_all invoked on breach, clean skip when `QDRANT_URL` unset.
7. **Spool core + drainer.** Files: `memorymaster/spool.py` (new), `memorymaster/jobs/spool_drain.py` (new), `memorymaster/cli_handlers_basic.py` (`drain-spool`), `memorymaster/service.py` (wire drain as cycle phase). Tests: `tests/test_spool.py` (new) — envelope round-trip, rename-before-read isolation, quarantine of unknown ops, replay idempotency (double-drain of same ingest line → 1 claim), **sensitivity filter fires on drained ingest lines** (red-bar a credential payload).
8. **RO recall + access spool.** Files: `memorymaster/service.py` (`_record_accesses` RO branch at `:1034`; store `read_only` plumb), `memorymaster/storage.py`, `memorymaster/context_hook.py` (RO store under flag), `memorymaster/config_templates/hooks/memorymaster-recall.py`. Tests: `tests/test_ro_recall.py` (new) — under flag: recall returns identical results vs RW on same fixture DB, zero write locks taken (assert via concurrent exclusive-lock probe), access lines land in spool, drain applies them via `record_accesses_batch` (`_storage_lifecycle.py:512`), tiering input preserved end-to-end (`recompute_tiers` sees the counts).
9. **Ambient-write spool.** Files: `memorymaster/config_templates/hooks/memorymaster-auto-ingest.py`, `memorymaster/config_templates/hooks/memorymaster-dream-sync.py`, `memorymaster/dream_bridge.py` (spool emit option). Tests: `tests/test_ambient_spool.py` (new) — hook templates write valid envelopes under flag, fall back to direct path with flag off, drained verbatim lands in `verbatim_turns` with identical rows to the direct path.
10. **init_db fast-path.** Files: `memorymaster/storage.py` (user_version stamp + skip), `memorymaster/migrations/` (stamp bump on new migration). Tests: `tests/test_initdb_fastpath.py` (new) — stamp mismatch forces full path, schema change without stamp bump is caught, fast-path init on stamped DB skips `_ensure_*` (assert via call counting). **Measure**: re-run the baseline cold-init benchmark, record in `PROGRAM-LOG.md`.
11. **Observability + installer.** Files: `memorymaster/jobs/integrity.py` (metrics emit), `memorymaster/dashboard.py` (WAL/spool/drift/busy panels), `scripts/setup-hooks.py` (regenerate hooks, set flag for hook processes), `memorymaster/retry.py` or `_storage_shared.py` (busy-error counter). Tests: `tests/test_integrity_metrics.py` (new) — events recorded per cycle, dashboard JSON includes the new fields.
12. **Chaos soak harness + run.** Files: `tests/soak/chaos_soak.py` (new, pytest-marked `soak`, excluded from default run), `scripts/run_chaos_soak.ps1` (new). Run against a **copy** of the live DB (never the live file) — design in §4. Gate: 0 quick_check failures across all rounds, flag on AND off.

## 4. Test plan

**Unit/integration** (per step, above): pragma parity sweep; ro-mode write rejection; integrity phase (checkpoint, quick_check sentinel + promotion freeze, vacuum rotation, throttles); fk repair (seeded 5-shape orphans, dry-run/apply/idempotent); qdrant reconcile (fake backend, drift threshold); spool (envelope, quarantine, double-drain idempotency, sensitivity red-bar); RO recall (result parity, zero write locks, access-signal preservation through drain to `recompute_tiers`); ambient spool parity; initdb fast-path safety. Tests anchor on requirements (e.g., "RO recall must still feed tiering"), not implementation.

**Chaos soak** (`tests/soak/chaos_soak.py`, the P1 exit gate):
- **Fixture**: copy a ~200 MB seeded slice of the real DB (schema-identical, built via `VACUUM INTO` from the live file read-only) to a temp dir per run.
- **12 simulated writers**, matching the real fleet shape: 6 × MCP-style ingest loops (`svc.ingest` with citations, fresh `MemoryService` per batch — mirrors `mcp_server.py:290-291`), 2 × recall loops (`query_for_context`, access recording on), 1 × Stop-hook loop (verbatim + learning ingest), 1 × dream-bridge loop, 1 × steward `run_cycle` loop (batch_limit small), 1 × `merge-db` loop against a sibling DB. Each writer journals every **acked** op (idempotency_key) to its own ledger file.
- **Kill rounds**: ≥20 rounds; each round runs 60 s of load, then `taskkill /F` (Windows kill -9) on 2-4 randomly chosen writer PIDs mid-flight, respawns them, continues.
- **After every round**: `PRAGMA quick_check` must return `ok`; `PRAGMA foreign_key_check` must return 0 rows; WAL size logged; acked-op ledgers reconciled against the DB (every acked ingest present exactly once — proves idempotency under kill).
- **Matrix**: flag OFF (regression guard for the legacy path) and flag ON (the new regime). Record busy-error counts both ways — expect ON ≤ OFF.
- **Pass = P1 gate**: 0 quick_check failures, 0 FK orphans, 0 lost acked writes, in both modes.

**Live verification after rollout** (per AGENTS.md): full suite + ruff + `run-cycle` on the real DB; re-measure recall p50/p95 and cold init against `.planning/BASELINE-2026-06-09.html`.

## 5. Rollout

- **Flag**: `MEMORYMASTER_WAL_DISCIPLINE` (umbrella: RO recall + spools; sub-flag `MEMORYMASTER_INITDB_FASTPATH`). **Default OFF** in code. The integrity steward phase, FK repair CLI, Qdrant reconcile, day-1 kills, and `open_conn` consolidation ship default-on (additive/bug-fix class, no behavior change to write semantics).
- **Day 0 (with merge)**: day-1 kills live; supervised `wal_checkpoint(TRUNCATE)` retires the 1.44 GB WAL (runbook); `repair-fk --apply` once (401 → 0, logged); VM crontab audited.
- **Dogfood**: `setx MEMORYMASTER_WAL_DISCIPLINE 1` (user-level — hooks pick it up immediately as fresh processes; panes inherit on natural respawn, mirroring the broker draft's no-.claude.json-change mechanism, F15). Soak **7 days** on this machine.
- **Flip criteria** (all from §2.10 dashboard metrics, checked at day 7): 0 quick_check failures; WAL ≤ 64 MB sustained across cycles; recall p50 ≤ 70.4 ms / p95 ≤ 117.6 ms (no regression vs baseline); spool drain lag ≤ 6 h with 0 quarantined lines; busy-error counter ≤ flag-off baseline; cold init < 2 s with fast-path on. Then default ON in v3.29 release.
- **Rollback**: `setx MEMORYMASTER_WAL_DISCIPLINE 0` (+ optionally rerun `scripts/setup-hooks.py` to restore prior hook templates). The legacy direct-write code is the intact else-branch everywhere; no schema change; any spool residue drains via one-shot `drain-spool` so no write is ever stranded. Integrity phases can be disabled individually via `MEMORYMASTER_INTEGRITY_DISABLE=1` if a checkpoint ever misbehaves.

## 6. What this deliberately does NOT do (accepted residuals)

- Panes remain RW writers → repair swaps still require pane shutdown (mitigated: snapshots + runbook; retired only by the daemon escalation).
- Ambient learnings gain up to 6 h visibility lag (steward drain cadence); shrinkable later with an opportunistic drain on MCP idle.
- The 1.2 s hook cold-import tax is untouched (that fix — stdlib HTTP hook — belongs to the daemon design; explicitly out of P1 scope).
- DB stays under `G:\_OneDrive\...` for now (F16): relocation via `MEMORYMASTER_DEFAULT_DB` is a config-only follow-up, scheduled right after flip when only a handful of writers hold handles.

## 7. Escalation tripwire (falsification criterion)

Escalate to the daemon design (P1.5) if, with the flag ON fleet-wide, ANY of: (a) a second btree corruption event; (b) `quick_check` failure not attributable to external interference; (c) WAL repeatedly > 256 MB because TRUNCATE never wins under pane churn (then first try a nightly quiesce step before full escalation); (d) busy-error counter trends up despite uniform 15 s timeouts. The §2.10 metrics make each of these observable rather than anecdotal.
