# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

## [3.22.0] - 2026-05-24

**Retrieval-quality release.** Ports the high-ROI ideas from a competitive
analysis of gbrain v0.40.8.1 (Garry Tan's agent brain) against our v0.22.4
baseline: a boost-gating fix for ranking, proactive semantic contradiction
detection, and a correctness-safe recall cache. All additions are opt-in /
default-off and backward compatible.

### Added

- **Floor-ratio boost gate (gbrain v0.35.6).** `MEMORYMASTER_BOOST_FLOOR_RATIO`
  (default 0.0 = off): metadata boosts (confidence/freshness/pinned/tier) only
  apply to candidates whose query-relevance (lexical+vector) is
  `>= ratio * top relevance`. Fixes the failure mode where a fresh/confident but
  topically-wrong claim outranks the true lexical match under a strong embedder.
  Implemented as a two-pass in `retrieval.rank_claim_rows`.
- **`query --explain` (gbrain v0.40.4).** Per-stage score attribution
  (relevance vs. boost terms, weights, floor-gate status) on every `RankedClaim`
  via a `breakdown` dict, rendered for evidence-based weight tuning.
- **Offline qrels retrieval-regression gate (gbrain v0.40.1).**
  `tests/fixtures/qrels_search.json` + `test_qrels_regression.py`: a
  deterministic, no-API top-1/recall@5 gate over a fixed corpus.
- **Semantic contradiction probe (gbrain v0.32.6).** New
  `memorymaster/contradiction_probe.py` + `detect-contradictions` CLI: samples
  topically-similar claim pairs in an embedding band (excluding the
  deterministic resolver's same-subject+predicate domain), LLM-judges genuine
  contradictions with severity, reports a Wilson-95%-CI rate (judge errors in
  the denominator), caches verdicts (migration `0003_contradiction_verdicts`),
  and `--apply` flags the lower-confidence claim as `conflicted`. H1-budget-capped.
- **Correctness-safe query cache (gbrain v0.40.3).** Opt-in
  `MEMORYMASTER_QUERY_CACHE=1` (SQLite-only). Migration `0004_query_cache` adds a
  `corpus_generation` counter maintained by column-scoped `claims` triggers (it
  excludes `access_count`/`last_accessed` so access recording doesn't
  self-invalidate). Cache keys fold in a config fingerprint, so any claim write
  or retrieval re-tune produces a miss + fresh compute — never stale.

### Fixed

- **De-rotted `test_cli_wiki_freshness_below_filter`.** It shelled out to the
  CLI (real wall-clock) but hardcoded `now=2026-04-24`; once wall-clock advanced
  it failed. Anchored the fixture to `datetime.now()`.

## [3.21.0] - 2026-05-21

**Rules + reliability release.** Bundles the v3.20.0 schema/sync work and the
v3.21.0 rule-shaped-claims line. Adds prescriptive "rule" claims, mines them
from user corrections (borrowing the learn-from-corrections idea from
ReflexioAI/claude-smart, natively), ships a versioned migration framework and
incremental delta-sync, and fixes a silent verbatim-capture bug. All additions
are opt-in / backward compatible.

### Added

- **Rule-shaped claims (R1a, PR #126).** New `memorymaster/rules.py`:
  prescriptive `when <trigger>, do <action> because <rationale>` claims stored
  as a normal claim with `claim_type="rule"` + JSON in `object_value` (no
  schema change — safe because deterministic value-validators are
  predicate-gated). New `ingest_rule` / `query_rules` MCP tools and
  `service.query_rules()`.
- **Verbatim correction miner (R1b PR1, PR #127).** New
  `memorymaster/rule_miner.py` + `mine-rules` CLI: scans the verbatim
  transcript archive for correction-signaled user turns (cheap SQL keyword
  pre-filter), distills each into a rule via an LLM, and ingests it as a
  low-confidence candidate. Bounded by the H1 per-cycle budget caps and
  resumable via a `miner_state` watermark. SQLite-only.
- **Ongoing correction mining (R1b PR2, PR #129).**
  `rule_miner.mine_transcript_rules()` called from the Stop hook mines each
  session's latest correction into a rule automatically (capped to one window
  per stop). Sensitive rules are dropped, not stored.
- **Versioned schema migrations (v3.20.0-S1, PR #119).** New
  `memorymaster/migrations/` with `MigrationRunner`, `schema_versions` table,
  sha256 drift detection, and a `migrate` CLI (`--list` / `--status`).
  Migration `0002_miner_state` adds the miner watermark table (both backends).
- **Incremental delta-sync (PR #121, #122, #123).** `export-delta` ships a
  small SQLite file of claims changed since a watermark, consumable by
  `merge-db` — cheap cross-machine sync without copying the full DB. Windows
  delta-sync script + watermark BOM-corruption fix.
- **SQLite/Postgres backend parity gate (v3.20.0-S2, PR #125).** New
  `parametrize_backends` fixture and cross-backend parity tests.

### Fixed

- **Verbatim capture silent-dropper (PR #128).** `store_transcript` read
  top-level `entry["role"]/["content"]`, but Claude Code transcripts nest both
  under `message` — so it captured zero real turns and zero roles (744k rows
  with `role=''`, only non-conversation metadata stored). Now unwraps
  `message.{role,content}` (with a legacy top-level fallback) and stores only
  user/assistant text turns. Prerequisite for the correction miner.

## [3.19.0] - 2026-05-17

**Phase 0 hardening release.** Closes the four security/ops gaps identified
in the GPT-5.4 review against the `docs/ROADMAP.md` Phase 0 list: LLM budget
caps (H1), dashboard auth (H2), webhook HMAC (H3), MCP path allowlist (H4).
All four mechanisms ship opt-in by default — no breaking behaviour changes
for callers that don't set the new env vars.

### Added

- **H1: Per-cycle LLM budget caps with reason-coded hard stops** (PR #113).
  New `memorymaster/llm_budget.py` contextvar-scoped tracker. Wraps
  `service.run_cycle`, `wiki_engine.absorb`, `jobs/daydream_ingest.ingest_insights`.
  Per-provider circuit breaker. Aborted runs surface `result["budget"]`
  with reason + counters + WARNING log.
- **H2: Dashboard HTTP auth + viewer/operator roles + CSRF + bind-safety**
  (PR #114). New `memorymaster/dashboard_auth.py`. Bearer-token auth via
  `hmac.compare_digest`. Operator-only routes block viewer at 403.
  Browser POSTs validated by Origin/Referer match. Non-loopback bind
  refuses without an auth secret unless `UNSAFE_BIND=1` explicit opt-in.
  Health endpoints exempt.
- **H3: Webhook HMAC-SHA-256 signing + timestamp + replay protection**
  (PR #115). `memorymaster/webhook.py` adds `X-MemoryMaster-Signature`
  and `X-MemoryMaster-Timestamp` headers on outbound webhooks when the
  secret is set. New `verify_webhook_signature` helper for receivers
  returns `(ok, reason)` with explicit `replay_window` / `bad_signature`
  / `missing_*` codes. 5-min default replay window.
- **H4: MCP db/workspace path allowlist + admin-mode bypass** (PR #116).
  New `memorymaster/mcp_path_policy.py`. Validates caller-supplied paths
  through every MCP tool via the single `_resolve_db` / `_resolve_workspace`
  chokepoint — zero invasive changes to 14+ tool entry points. Glob
  patterns (`fnmatch`) supported. Both denial and admin-mode bypass log
  structured WARNING with `actor=mcp_caller` for audit.

### Env vars reference

| Env var | Default | Purpose |
|---|---|---|
| `MEMORYMASTER_MAX_LLM_CALLS_PER_CYCLE`        | `0` (unlimited) | H1 cycle call cap |
| `MEMORYMASTER_MAX_TOKENS_PER_CYCLE`           | `0` (unlimited) | H1 cycle token cap |
| `MEMORYMASTER_MAX_PROVIDER_FAILURES_PER_CYCLE` | `0` (unlimited) | H1 per-provider breaker |
| `MEMORYMASTER_DASHBOARD_TOKEN_VIEWER`         | unset (legacy)  | H2 read-only bearer |
| `MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR`       | unset (legacy)  | H2 mutating bearer |
| `MEMORYMASTER_DASHBOARD_UNSAFE_BIND`          | unset (refuse)  | H2 non-loopback escape |
| `MEMORYMASTER_WEBHOOK_SECRET`                 | unset (no sig)  | H3 HMAC signing key |
| `MEMORYMASTER_MCP_DB_ALLOWLIST`               | unset (allow all) | H4 DB path allowlist |
| `MEMORYMASTER_MCP_WORKSPACE_ALLOWLIST`        | unset (allow all) | H4 workspace allowlist |
| `MEMORYMASTER_MCP_ADMIN_MODE`                 | unset (enforce) | H4 allowlist bypass |

### Tests

63 new tests across H1/H2/H3/H4:

- `tests/test_llm_budget.py` — 8 tests
- `tests/test_dashboard_auth.py` — 25 tests (19 unit + 6 end-to-end HTTP)
- `tests/test_webhook_hmac.py` — 13 tests
- `tests/test_mcp_path_policy.py` — 17 tests (12 unit + 5 chokepoint integration)

Zero regressions on pre-existing webhook / dashboard / mcp_helpers / daydream
test suites. Full pytest stays green at each merge.

### Notes

- **Phase 1 (storage discipline, migrations + parity gate) is the next
  release target** per ROADMAP. Per-bucket retrieval profile tuning
  intentionally deferred (only ~+0.013 R@5 ceiling left, diminishing
  returns vs hardening leverage).
- **A1 full LongMemEval-S QA-accuracy publication run still pending** —
  mechanism shipped in v3.18.0 (PR #109), safer to run now that v3.19.0-H1
  budget caps prevent runaway provider failures.

## [3.18.0] - 2026-05-17

### Added

- **Per-question-type retrieval weight profiles (S3, #110).** New `MEMORYMASTER_RETRIEVAL_PROFILE_<TYPE>=lex,conf,fresh,vec` env-var family lets retrieval swap the hybrid blend weights per question type. `Config.retrieval_profile(qtype)` lookup, `MemoryService.query/query_rows` forward an optional `query_type`, `tests/bench_longmemeval.py` passes `item['question_type']` from the LongMemEval-S dataset. Mechanism is opt-in and isolated — when no profile env is set, behavior is unchanged.

- **`claude_cli` judge provider for bench (A1, #109).** Fourth provider in `tests/bench_longmemeval.py:JudgeClient` routes through `memorymaster.llm_provider._call_claude_cli`, shelling out to local `claude --print` over Claude Code OAuth. Unblocks the full LongMemEval-S QA-accuracy bench for environments with no API keys.

  ```bash
  MEMORYMASTER_LLM_MODEL=claude-sonnet-4-5 \
    python tests/bench_longmemeval.py --full --judge claude_cli \
    --judge-pacing-seconds 0 --qa-max-seconds 30000
  ```

### Improved

- **LongMemEval-S R@5: 0.966 → 0.972 (+0.006)** with the validated `single-session-preference=0.10,0.10,0.10,0.70` profile. Per-bucket apples-to-apples on same code: preference R@5 0.8000 → 0.9000 (+0.10, +12.5% relative); every other bucket unchanged (0.0000 drift). First non-NULL retrieval improvement since v3.15.0. Details: `docs/v318-experiments/E01-results.md`.

### Notes

- `docs/longmemeval-results.md` per-bucket table is stale (lists preference at R@5=0.40; real current baseline is 0.80). Headline overall R@5 numbers still accurate. Will refresh on next bench publication pass.
- S3 E02 (temporal-reasoning fresh-heavy) NULLed — bench's W_FRESH axis is degenerate because all sessions ingest in a tight wall-clock window so freshness anchor is uniform.
- A1 full 500q overnight run not included in this release — proven mechanism, but full publication run deferred.

## [3.17.1] - 2026-05-16

### Added

- **Steward auto-ingest hook for daydream**: `run_steward()` now optionally calls `ingest_insights()` at the end of its cycle, so daydream's accepted markdown notes flow into MemoryMaster as candidate claims without manual `ingest-daydream` invocation. Zero-touch closure of the vault → daydream → claims → wiki loop on the existing 6h steward cron.

### Safety

The hook is opt-in (default OFF) and error-isolated:

- **Default OFF.** Activates only when `MEMORYMASTER_DAYDREAM_INGEST_DIR=<path>` is set.
- **Try/except wrap.** Any exception in the daydream ingest is recorded in the steward result dict under `result["daydream"]["error"]` but never propagates — the steward cycle's other work (validation, decay, compaction) always completes.
- **Last step.** Hook fires AFTER existing steward work, so even a hook failure doesn't lose the cycle's main output.
- **Quiet by default.** Log lines emit only when `MEMORYMASTER_DAYDREAM_VERBOSE=1`.
- **Graceful no-op.** Skips silently when env var unset, dir missing, dir empty, or no insights pass threshold.

4 new tests in `tests/test_steward_daydream_hook.py` cover all four safety paths, including the critical "ingest exception doesn't break steward" case.

### Use

```bash
# One-time setup
export MEMORYMASTER_DAYDREAM_INGEST_DIR="/path/to/vault/Daydreams"
export MEMORYMASTER_DAYDREAM_VERBOSE=1   # optional, default quiet

# That's it — the existing steward 6h cron now also auto-ingests daydream insights
```

## [3.17.0] - 2026-05-16

### Headline

**Daydream → MemoryMaster claims pipeline.** Closes the loop: vault → daydream synthesizer → MemoryMaster candidate claims → steward validation → wiki-absorb → vault.

### Added

- `memorymaster/jobs/daydream_ingest.py` — reads accepted insights from the [glebis/daydream](https://github.com/glebis/claude-skills/tree/main/daydream) skill's output directory (markdown notes with frontmatter under `<vault>/Daydreams/`). For each insight, creates a candidate claim with:
  - `claim_type = "hypothesis"`
  - `confidence = 0.5`
  - `source_agent = "daydream"`
  - `subject` from the insight title
  - `text` from the synthesis body
  - Citations linking back to the source notes
- CLI subcommand: `python -m memorymaster --db <db> ingest-daydream <insights-dir> [--min-score N] [--dry-run]`
- 6 tests covering: above-threshold ingest, dry-run, idempotent re-ingest, malformed-input tolerance, claim-shape correctness, and citation linkage.
- `docs/daydream-integration.md` — short usage doc.

### Why this matters

The user has a 2,891-note Obsidian vault with notes spanning multiple projects (memorymaster, mzcopilot, Pather, others). At that scale, manually finding cross-pollination connections is impossible. Daydream samples 50 random recency-weighted note pairs daily and surfaces the surviving ≥7.0-rated insights. Without this ingest pipeline those insights would just accumulate as dated daily notes; with it, they become first-class claims that the steward can validate, the wiki can absorb, and the recall hook can surface.

### Use

```bash
# 1. In any Claude Code session inside the vault
/daydream

# 2. Then ingest the new insights
python -m memorymaster --db memorymaster.db ingest-daydream obsidian-vault/Daydreams

# 3. Steward will validate on its 6h cycle; wiki-absorb weaves
#    confirmed insights into the wiki on the same cycle
```

Optional: chain step 2 into a hook so daydream's output auto-ingests.

## [3.16.0] - 2026-05-14

### Headline

LongMemEval-S R@5 unchanged at **0.966** — same level as v3.15.0, still leading agentmemory's published 0.952. v3.16.0 ships the **architectural unblock for future retrieval tuning** plus a documented honest-null on RRF-as-tiebreaker.

### Methodology

Two experiments dispatched per the v3.16 roadmap (`docs/v316-roadmap.md`). Same release-discipline pattern as v3.15.0: each experiment is a measurement against a fixed metric, honest-null acceptance, plateau-stop on diminishing returns.

### Experiments (PRs #102-#103)

| # | Experiment | Verdict | R@5 |
|---|---|---|---|
| S1 | Unify W_LEX/W_VEC/W_CONF/W_FRESH constants across `retrieval.py`'s lexical-only and semantic-aware ranking paths | **KEEP** (architectural neutral) | 0.966 (unchanged) |
| S2 | RRF as tiebreaker for near-tie pairs (within 0.01 score gap) over the 4 component rankings | NULL | 0.966 (unchanged) |

### What S1 unblocks

Before S1, the `MEMORYMASTER_RETRIEVAL_WEIGHTS` env override only reached the lexical-only ranking path — the semantic-aware hybrid path had hardcoded weights. This made every weight-tuning experiment unreliable (E05 in v3.15.0 REVERTed because of this). S1 threads a single canonical weight source through both paths, with a regression test (`tests/test_retrieval_weights.py`) that asserts the override now affects both. Future weight sweeps will produce trustworthy deltas.

### Why S2 is a NULL

The RRF tiebreaker activates on near-tie pairs as designed (3 unit tests pass) but at R@5 = 0.966 the top-5 ranking is already well-determined by the linear blend — reshuffles within 0.01-score neighborhoods don't change which sessions land in top-5. Production default stays OFF; the flag is opt-in for future experiments where the score distribution might be flatter.

### Architecture findings

- The override-bench from S1 (with `MEMORYMASTER_W_LEX=0.55`) confirmed E05's finding: bumping W_LEX HURTS at vector-enabled baseline (R@5 drops to 0.956). The dominant-lever hypothesis for S3 (per-question-type retrieval profiles) is now known-bad for the W_LEX axis. S3 demoted from "highest-leverage" to "small experiment worth one shot".
- The path forward for non-trivial R@5 gains is not in fusion/ranking tuning. The next genuinely meaningful work is **A1 (full QA judge accuracy)** — blocked on `ANTHROPIC_API_KEY` configuration in the shell env.

### Comparison to industry baseline

| | v3.16.0 | agentmemory (published) | Δ |
|---|---|---|---|
| R@5 | **0.966** | 0.952 | +0.014 ★ |
| R@10 | 0.984 | 0.986 | -0.002 |
| MRR | **0.902** | 0.882 | +0.020 ★ |

v3.16.0 maintains v3.15.0's lead on R@5 and MRR.

## [3.15.1] - 2026-05-14

### Added

- **README benchmarks section** — embedded SVG bar chart comparing v3.14, v3.15, and agentmemory across R@5 / R@10 / MRR, plus a comparison table. v3.15.0 leadership on R@5 (+0.014) and MRR (+0.020) is now visible at first glance on the repo home page.
- `docs/benchmark-longmemeval.svg` — the chart asset, inline-renders in GitHub.
- `docs/v316-roadmap.md` — concrete next-step improvement levers ranked by predicted Δ R@5: S1 unify weight constants across ranking paths (prereq), S2 RRF-as-boost not base fusion, S3 per-question-type retrieval profiles, A1 full QA-judge pass, A2 LongMemEval-M, A3 trigram lexical signal, plus deferred B-tier items.

### Notes

Docs-only release. Production retrieval code unchanged from v3.15.0. The R@5 = 0.966 number stands.

## [3.15.0] - 2026-05-14

### Headline

**LongMemEval-S R@5: 0.894 → 0.966 (+0.072)** — driven entirely by E01 (bench harness wiring). v3.14 production retrieval code was always capable of this number; the gap was a measurement bug in the previous bench harness. See `docs/longmemeval-results.md`.

### Methodology

Followed `memorymaster-release-discipline` skill: each experiment was a measurement against a fixed metric (LongMemEval-S R@5/R@10/MRR on full 500q), with honest-null and honest-harm acceptance. 6 experiments dispatched, 1 KEEP, 2 REVERT, 3 NULL. Plan file: `~/.claude/plans/witty-stargazing-mango.md`.

### Experiments (PRs #94-#99)

| # | Experiment | Verdict | R@5 vs 0.894 baseline |
|---|---|---|---|
| E01 | Enable vector signal in bench (sentence-transformers all-MiniLM-L6-v2) | **KEEP** | **+0.072 → 0.966** |
| E02 | RRF fusion (k=60) in `query_rows()` | REVERT | -0.046 (0.920) |
| E04 | Session-diversity reranker (max-3-per-source) | NULL | 0.000 |
| E05 | W_LEX sweep {0.50, 0.55, 0.60} | REVERT | -0.022 (0.944) — env override didn't reach semantic branch |
| E06 | Cross-encoder rerank via gemini-2.5-flash | NULL | 0.000 (Gemini quota cap, only 3/500 reranked) |
| E08 | FTS5 porter stemming tokenizer | NULL | 0.000 |

(E03 entity-graph stream was skipped: depended on broken RRF base from E02. E07 zero-LLM graph extraction deferred — cost-only, not R@5.)

### Architecture findings (honest-null docs are commits in main)

- **The bench had vector signal structurally suppressed** before E01. `tests/bench_longmemeval.py` popped `QDRANT_URL` and called `query_rows(retrieval_mode="legacy")`. v3.14's published 0.894 number was a measurement artifact, not a model ceiling.
- **Fusion-layer changes don't help post-E01.** Three independent attempts (RRF, diversity cap, weight tuning) all REVERT or NULL — the linear blend (W_LEX=0.45, W_CONF=0.30, W_FRESH=0.15, W_VEC=0.10) is at a local optimum once real semantic embeddings are enabled.
- **`retrieval.py` has two ranking paths that don't share weight constants.** Lexical-only path uses `MEMORYMASTER_RETRIEVAL_WEIGHTS`; semantic-aware hybrid path has hardcoded blending. E05 surfaced this — flagged as architectural debt for follow-up.

### Comparison to industry baseline

| | MemoryMaster v3.15.0 | agentmemory (published) | Δ |
|---|---|---|---|
| R@5 | **0.966** | 0.952 | **+0.014** |
| R@10 | **0.984** | 0.986 | -0.002 |
| MRR | **0.902** | 0.882 | **+0.020** |

MemoryMaster now leads on R@5 and MRR with retrieval-only. QA accuracy (with judge) deferred — Anthropic/OpenAI/Gemini judge quotas all saturate before any meaningful judging completes on this account.

## [3.14.0] - 2026-05-11

**Codex parallel-burn reliability and documentation release.** Consolidates the PR #31-#43 batch: full-content verbatim deduplication, safer MCP input handling, provider-routed LLM resolution, bounded dedup operations, and expanded test/documentation coverage.

### Features

- **MCP validation and sensitivity guard** (#32) -- adds Pydantic input validation, structured errors, and a sensitivity guard so unsafe ingest payloads are rejected before entering MemoryMaster.
- **Incremental dedup controls** (#36) -- adds `--limit` and `--scope` CLI flags so dedup runs can be bounded by batch size and project scope.

### Fixes

- **Verbatim store hybrid-merge dedup** (#31) -- switches hybrid-merge dedup from prefix keys to full-content hashes to avoid false duplicate matches.
- **Auto-resolver provider routing** (#33) -- routes `_llm_evaluate` through `llm_provider.call_llm` instead of bypassing the shared provider abstraction.
- **Google key rotation** (#38) -- wires `KeyRotator` into `_call_google` so Gemini calls can rotate across configured environment-provided keys.
- **Qdrant verbatim dedup keys** (#43) -- extends full-content hash dedup to Qdrant point IDs so vector-store verbatim records avoid prefix-collision failures.

### Tests

- **Embedding tests** (#34) -- mocks Gemini embedding calls to keep deprecated `text-embedding-004` behavior from breaking the suite through live provider calls.
- **Decay coverage** (#37) -- adds focused regression coverage for claim decay behavior.
- **Dashboard coverage** (#39) -- adds dashboard route coverage.

### Docs

- **Storage parity audit** (#35) -- documents the SQLite/Postgres parity audit for storage behavior.
- **Dedup-key collision audit** (#40) -- adds the 2026-05-11 dedup-key audit documenting prefix-collision risk and remediation.
- **Cookbook from gotcha claims** (#41) -- compiles operational gotchas and bug root causes from MemoryMaster claims into cookbook material.
- **ADRs from decision claims** (#42) -- compiles architecture decision records from MemoryMaster decision claims.

## [3.13.0] - 2026-04-29

**Pre-steward candidate dedupe.** Adds a Jaccard-on-tokens dedupe tier that runs *before* the steward LLM. Candidates that overlap >= 85% with an existing same-scope claim get archived via SQL with no Haiku call. Inspired by Mendral's "We Upgraded to a Frontier Model and Our Costs Went Down" (2026-03-06): cheap-deterministic-first, expensive-LLM-second.

Default OFF (`MEMORYMASTER_DEDUPE_ENABLED=0`). When enabled, default is shadow mode (`_SHADOW=1` — counts but does not act). Operators flip both off after one observation cycle.

### Added

- **`memorymaster/candidate_dedupe.py`** — `find_near_duplicate(conn, *, candidate_id, candidate_text, candidate_scope)`. Two-stage: FTS5 OR-query top-K filter, then token Jaccard score. Returns archive when the best canonical match scores >= threshold; otherwise passthrough.
- **`memorymaster/llm_steward.py`** — wires dedupe into `run_steward()` between the too-short check and `extract_claim()`. Adds `dedupe_enabled`, `dedupe_shadow`, `dedupe_archived`, `dedupe_would_archive`, `dedupe_passthrough`, `dedupe_avg_jaccard` to the stats dict.
- **`scripts/measure_dedupe_thresholds.py`** — sweeps thresholds [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95] over the live candidate set, prints archive count + score distribution + sample pairs for manual precision check.
- **`tests/test_candidate_dedupe.py`** — 14 unit tests covering env flags, FTS5 absent, scope isolation, archived-excluded, threshold gating, candidate-vs-candidate, Jaccard math, immutability.
- **`tests/test_v313_e2e.py`** — 4 E2E tests through `run_steward`: archives paraphrase without LLM, disabled-passes-to-LLM, shadow-mode-doesn't-act, 30-paraphrase synthetic corpus.

### Env flags (defaults)

| Var | Default | Meaning |
|-----|---------|---------|
| `MEMORYMASTER_DEDUPE_ENABLED` | `0` | Off until operator opts in |
| `MEMORYMASTER_DEDUPE_SHADOW` | `1` | Count would-archive without acting |
| `MEMORYMASTER_DEDUPE_JACCARD_HIGH` | `0.85` | Token-set Jaccard threshold |

### Threshold tuning evidence

Sweep over 2,000 candidates against the live 14k-claim corpus:

| threshold | archive rate | precision (n=10 sample) |
|-----------|-------------:|-------------------------|
| 0.95      |  0.3%        | (n too small)           |
| 0.90      |  2.0%        | 100% (subset of 0.85)   |
| **0.85**  |  **3.7%**    | **100%**                |
| 0.80      |  6.5%        | ~95% (samples 1-10 ok)  |
| 0.75      |  9.3%        | ~80% (some borderline)  |
| 0.70      | 11.6%        | not validated           |

0.85 chosen as default for high-precision conservative archive. Lower thresholds available via env override; precision drops gradually but recall climbs ~2× per 0.05 step.

### Why Jaccard, not BM25

Initial implementation used SQLite FTS5 `bm25()` directly. BM25 collapses to ~0 on small corpora (IDF degenerates when most tokens appear in every doc). Token Jaccard is corpus-independent and behaves the same on a 2-doc test fixture and a 14k-claim DB. FTS5 still narrows the search space cheaply; Jaccard scores the final match.

### Why this is shipped OFF by default

The post-LLM (subject, predicate) dedup at `llm_steward.py:627-643` is the existing safety net — every paraphrase that passes through pre-LLM dedup still gets a chance to dedup after extraction. Pre-LLM dedup is purely an optimisation, never load-bearing for correctness. Operators with no Haiku-cost concern can leave it off forever.

### Architecture compliance

- No schema migration. Reuses existing `claims_fts` virtual table + `replaced_by_claim_id` FK.
- SQLite-only. Postgres backend has no FTS at all (no `tsvector`); dedup is a no-op there. No parity drift since no new columns.
- All 1,796 existing tests still pass. 14 + 4 = 18 new tests added.

## [3.12.0] - 2026-04-27

**Definitive end of the recall-feature track.** v3.10 hypothesised the labeled GT was too narrow to detect lift from new candidates. v3.12 tested it: re-labeled 953 prompts against the top-50 candidates per prompt (was top-15). Result: baseline jumps **0.104 → 0.470** (+0.366), confirming the GT-coverage bottleneck was real. **But every v3.9-v3.11 feature is STILL NEGATIVE on the wider GT.** F1 −0.001, F6 boost-only −0.005 to −0.013, F5+F8 −0.033 to −0.058. The features don't aport signal at top-5 on this corpus, regardless of label coverage.

Defaults stay at 0.0 / OFF across F1/F5/F6/F8. The machinery + tests + new wider GT ship as infrastructure for future investigations on different corpora.

### Added

- **`artifacts/real-prompts-1000-top50.jsonl`** + **`artifacts/real-prompts-1000-top50-labels.json`** — the new wider GT. 953 prompts, **646 non-empty labels (67.8%)**, 2,734 total label IDs (avg 4.2 per non-empty prompt). Generated by 5 parallel haiku subagents over precomputed top-50 candidates.
- **`artifacts/label-batches-top50/`** — raw per-chunk in/out JSONs for reproducibility.
- **`artifacts/recall-measurement-top50-2026-04-27.md`** — full sweep results with the new baseline + decisions + interpretation.

### Confirmed

- The GT-coverage hypothesis was REAL: top-15 GT depressed baseline from 0.470 to 0.104 because most labelled-correct claims were below the rank-15 cut.
- The recall-feature hypothesis is REFUTED: F1/F5/F6/F8 don't improve precision@5 on this corpus even when labels capture them. Three release cycles (v3.10, v3.11, v3.12) converged on the same null/negative conclusion.

### Deferred to future research

The v3.13+ track moves AWAY from re-ranking the existing claim set (no signal there). Higher-ROI directions:

1. **Real-world recall capture** — instrument the recall hook to log `(query, returned-IDs, user-clicked-IDs)` to a separate corpus. Eval against THAT data, not synthetic prompts.
2. **Vector recall (W_VECTOR)** — currently 0.0 because no Qdrant index. The vector channel is a different signal source than entity-fanout / structural edges.
3. **Compaction / dedup** — reduce corpus noise (5,000+ near-duplicate claims) rather than re-rank what's there.

### Notes

- All features from v3.9-v3.11 remain in-tree and env-gated. No code removed. A user with a different corpus (less lexically-clean queries) may still get value.
- The 646-non-empty top-50 labels are a stronger eval set than the 248-non-empty top-15 labels. Future MemoryMaster recall work should baseline against the new file.
- Pure additive — no breaking changes, no schema changes.

## [3.11.0] - 2026-04-27

Three v3.10 follow-ups shipped — closets BM25-scaled + boost-only mode, F1 swap to query_classifier, F8 shares_entity edges. **Eval ceiling identified**: the labeled GT in `artifacts/real-prompts-1000-labels.json` was generated by LLM-judging the top-15 of CURRENT recall, so any feature that adds NEW candidates to top-5 (claims not in the original top-15) counts as a false positive by construction. None of the v3.9 features can show a positive lift on this eval until the labeling is widened. The fixes ship anyway because (a) they remove real bugs, (b) they make the code safe for users to enable without silent harm, (c) when a future re-label captures more ground truth, the features will work without re-touching the code.

### Added

- **P1 — F6 closets BM25-scaled scoring**. `search_closets()` gained a `with_scores=True` parameter that returns 3-tuples `(slug, claim_ids, score)` where `score` is FTS5 BM25 normalised so the best match scores 1.0 and weaker matches scale below. Fixes the v3.10 silent-1.0 bug that flooded top-5 with article-membership noise. Backwards-compat: legacy 2-tuple call still works.
- **P1 — `MEMORYMASTER_RECALL_CLOSETS_BOOST_ONLY=1`** env-gate. When set, closets only RE-RANK already-recalled rows (never hydrate new candidates). Stricter MemPalace "boost signal, never gate" interpretation. Recovers from `precision@5=0.060` (v3.10 W=1.0, harmful) to `0.096` (v3.11 boost-only W=1.0). Default remains OFF for back-compat with the v3.10 wiring.
- **P1 — Closets candidate cap reduced from 5 to 3 articles per query** when invoked from the recall hook. Bounds the candidate flood.
- **P2 — F1 swap to `query_classifier.classify_query`** (was `classify_observation`). Replaces the 6-pattern observation classifier with the 7-pattern query classifier and an explicit `query_type → claim_type` mapping. Falls back to `classify_observation` if `query_classifier` import fails.
- **P3 — F8 `shares_entity` edge kind** (`memorymaster/claim_edges.py`). New constant `SHARES_ENTITY_KIND = "shares_entity"`. `rebuild_edges()` gained `include_shares_entity=True` (default) + `shares_entity_max_per_pivot=50` parameters. For every entity_id with 2-N claims (N=cap), emits pairwise edges so the F5+F8 walker can traverse the entity-mediated graph. Lifts coverage on the live DB from **462 → 3,093 edges** (143 mention + 319 supersession + **2,631 shares_entity**). Edge kinds are independent — callers can still query by kind.
- Tests: `tests/test_v311_fixes.py` (10 cases) cover P1 BM25 normalisation + boost-only flag, P2 classify_query smoke, P3 shares_entity edge generation + cap behaviour.

### Measurement (vs N=953 baseline 0.104)

| Config | precision@5 | Δ |
|---|---|---|
| F6 closets W=1.0 v3.10 (constant) | 0.060 | -0.044 |
| F6 closets W=0.1 v3.11 (BM25) | 0.086 | -0.018 |
| F6 closets W=1.0 v3.11 (BM25) | 0.063 | -0.041 |
| F6 closets W=0.1 v3.11 boost-only | **0.098** | **-0.006** |
| F6 closets W=1.0 v3.11 boost-only | **0.096** | **-0.008** |
| F1 W=0.5 v3.11 (query_classifier) | 0.098 | -0.006 |
| F5+F8 W=0.1 (with shares_entity) | 0.094 | -0.010 |
| Combined v3.11 | 0.091 | -0.013 |

P1 boost-only mode recovers most of the closets regression. None of the re-runs cross the baseline because of the GT-coverage ceiling explained above.

### Notes

- All defaults remain at 0.0 / OFF. Users who enable closets in good faith now get `BOOST_ONLY=1` as the safe path documented in the CHANGELOG; the original "hydrate new candidates" path stays available behind the same env-gate without the new flag.
- Defining a re-labeled GT (LLM-judge over top-50 instead of top-15, or human-labeled subset) is the v3.12 unblocker. Without it, no recall feature can demonstrate positive lift on this eval set.

## [3.10.0] - 2026-04-27

Honest-null + honest-negative release. Wired F6/F8 into the recall hook (so they're actually consumed, not just dormant tables) and measured the v3.9.0 features against the N=953 prompt set. Result: F1/F5/F8 produce null deltas (±0.001). **F6 is actively harmful** with the constant-score scoring (-0.018 to -0.044 absolute precision@5). Defaults stay at 0.0 across all four new weights; all three new env-gates default to OFF. Legacy ranking remains bit-identical. Full report in `artifacts/recall-measurement-v3.10-2026-04-27.md`.

### Added

- **W1 — Closets stream wired into `context_hook.recall()`** (env-gate `MEMORYMASTER_RECALL_CLOSETS=1`). Calls `search_closets(query)`; for each matched wiki article, hydrates its `claim_ids` as new rows annotated `closet_score=1.0`, or boosts the score on already-recalled rows. New `W_CLOSETS` weight (default 0.0). Tests: `tests/test_closets_recall_integration.py` (+ extended).
- **W2 — F8 claim_edges walker wired into the F5 two-pass stream** (env-gate `MEMORYMASTER_RECALL_TWO_PASS_USE_EDGES=1`). When the two-pass stream is on AND this flag is set, the structural-edges BFS runs alongside the entity-fanout walk. Distances are minimised across the two sources so the closer reference wins.
- **F5 two-pass score is now distance-decayed** (`1/(1+hops)`) instead of constant 1.0. Only matters when `_two_pass_use_edges()` is on; the entity-fanout walker still produces hop=1 distances so behaviour is unchanged for `RECALL_TWO_PASS=1` alone.
- **`artifacts/recall-measurement-v3.10-2026-04-27.md`** — full sweep results + decisions + next-levers for v3.11.

### Fixed (S items from v3.9.0 F9 audit)

- **S1 — verbatim_recall import failure now logs WARNING once** at module load (`memorymaster/context_hook.py`). Previously silently fell back to no-op lambdas; users couldn't tell that even with `MEMORYMASTER_RECALL_VERBATIM=1` set the stream was effectively OFF. New module-level `_VERBATIM_IMPORT_WARNED` flag prevents log spam.
- **S2 — claim_edges missing-table now logs WARNING once** in `walk_neighbors` (`memorymaster/claim_edges.py`). Same pattern: module-level `_MISSING_TABLE_WARNED` flag, hint to run `rebuild_edges()`. Previously silently returned `{}` so callers didn't know to bootstrap.
- Tests: `tests/test_v391_strict_warnings.py` (6 cases) verify both warnings fire once and only once per process.

### Measurement results (vs N=953 baseline precision@5=0.104)

| Config | precision@5 | Δ |
|---|---|---|
| baseline | 0.104 | — |
| F1 W=0.3 | 0.104 | 0.000 |
| F5 entities W=0.3 | 0.103 | -0.001 |
| F5+F8 edges W=0.3 | 0.103 | -0.001 |
| F6 closets W=0.1 | **0.086** | **-0.018** |
| F6 closets W=1.0 | **0.060** | **-0.044** |
| Combined | 0.086 | -0.018 |

**F6 root cause:** `closet_score = 1.0` constant means each of the 5 closet hits × ~10 claim_ids/article = ~50 candidates flooding the top-5 with article-membership noise. The fix is to scale by FTS5 BM25 score (deferred to v3.11). The env-gate kept users safe in v3.9.0 because the wiring didn't exist; v3.10.0 ships the wiring with the env-gate still defaulting OFF.

### Notes

- Pure additive — no schema migrations applied automatically. Run `rebuild_edges` / `rebuild_closets` explicitly to populate the new tables.
- v3.11 priorities: (a) F6 BM25-scaled scoring fix (highest), (b) F1 swap to `query_classifier.py` instead of `classify_observation`, (c) F8 entity-mediated edge kind to lift coverage from 2.7% to ~50%.
- The negative results are themselves a deliverable — they prevent silent regressions for users who would have enabled the streams in good faith.

## [3.9.0] - 2026-04-27

"Steal everything good" release. Surveyed 6 active memory/code-graph projects (gbrain, MemPalace, graphify, claude-mem, GitNexus, My-Brain-Is-Full-Crew), identified 9 portable features, shipped them all in one release with full unit + E2E coverage. Survey doc: `artifacts/steal-from-others-2026-04-27.md`. Roadmap doc: `artifacts/roadmap-v3.9.0-2026-04-27.md`.

**Stats:** +9 features. +95 new tests (89 unit + 6 E2E). 1754 regression tests still green. Zero breaking changes. All new behaviour env-gated by default.

### Added

- **F1 — claim_type-aware ranking** (MemPalace "Halls"-inspired). New `W_CLAIM_TYPE` weight in `context_hook._RECALL_WEIGHT_DEFAULTS`. When > 0 (default 0.0), the recall hook classifies the query via `classify_observation()` and boosts rows whose `claim.claim_type` matches by `(1 + w_claim_type)`. Tests: `tests/test_claim_type_ranking.py` (6 cases).
- **F2 — MemPalace-style CamelCase library_name extraction** (`memorymaster/entity_extractor.py`). New Layer-1 regex `_CAMEL_LIB_RE` catches multi-cap names (`MemPalace`, `OpenAI`, `OneSignal`) and tech-suffixed names (`ChromaDB`, `FastAPI`, `NextJS`) without fragmenting. Stoplist filters false positives (`OneDrive`, `GitHub`, `WhatsApp`). Tests: `tests/test_entity_regex_v3.py` (7 cases).
- **F3 — `memorymaster/scope_utils.py`** new module. `scope_from_cwd()`, `cwd_from_transcript()`, `scope_from_transcript()` — read the authoritative `cwd` from a Claude Code session JSONL instead of slug-decoding the encoded folder name. Solves the same drift class as the v3.3.1 hash-suffix bug, but at the right layer. Tests: `tests/test_scope_utils.py` (14 cases).
- **F4 — `memorymaster/wiki_validate.py`** new module + CLI. `validate_file()`, `auto_fix()`, `audit()`, plus a `main()` argparse entry-point usable as `python -m memorymaster.wiki_validate <path> [--fix] [--audit] [--json]`. Auto-fixes the 4 fixable codes (`MISSING_OPEN`, `MISSING_CLOSE`, `EMPTY_FRONTMATTER`, missing `description`/`date`/`tags`/`title`). Creates `.bak` backup before write. Ported from gbrain v0.22.4. Tests: `tests/test_wiki_validate_cli.py` (13 cases).
- **F5 — Two-pass entity-fanout retrieval** (env-gated, gbrain v0.21 "Cathedral II"-inspired). New `MEMORYMASTER_RECALL_TWO_PASS=1` enables a second pass that fans out via `claim_entities` to find neighbour claims of already-recalled seeds. New `W_TWO_PASS` weight (default 0.0). Defensive: missing tables → `[]` not crash. Tests: `tests/test_two_pass_recall.py` (10 cases).
- **F6 — `memorymaster/closets.py`** new module. Search-side wiki-pointer boost (MemPalace v3.3.0 "Closets" pattern). New `closets` + `closets_fts` tables. `extract_closet_terms()`, `rebuild_closets()`, `search_closets()`. Closets are populated from regex-extracted CamelCase / fenced-code / wikilinks / bare words in article bodies; search hits closets first as boost, claims direct stays as floor. Tests: `tests/test_closets.py` (12 cases).
- **F7 — `memorymaster/federated_graphify.py`** new module (graphify v0.5.0 `merge-graphs` pattern). `discover_graphify_projects()`, `load_graph()`, `merge_graphs()`, `federated_query()` — walk N project roots, merge their `graphify-out/graph.json` outputs with per-node `repo` tag, query across the federated graph. Tests: `tests/test_federated_graphify_mcp.py` (13 cases).
- **F8 — `memorymaster/claim_edges.py`** new module (gbrain v0.21 "Cathedral II" structural-edges pattern). New `claim_edges` table with composite primary key. `extract_edges_for_claim()` finds `claim NNNN` and `mm-<hex>` references; `rebuild_edges()` walks the entire claims table to populate; `walk_neighbors()` does BFS with `max_hops` and `direction={out,in,both}`. Supersession edges (from `claims.replaced_by_claim_id`) included for symmetric walks. Tests: `tests/test_claim_edges.py` (13 cases).
- **F9 — `artifacts/cynical-deletion-audit-2026-04-27.md`** (claude-mem v12.4.7 PR #2141 pattern). Audit of all 36 silent `try/except/pass` blocks in `memorymaster/*.py`. Classified: 18 KEEP (legitimate defenders with explicit invariant), 10 DOCUMENT (need a `# why:` comment in v3.9.1), 8 REVIEWED with 2 marked STRICT for v3.9.1 follow-up.
- **End-to-end smoke** (`tests/test_v390_e2e.py`, 6 cases). Exercises F2/F3/F4/F6/F8 against a real DB + temp vault and verifies module imports have no circular dependencies.

### Changed

- `memorymaster/context_hook.py` — wired `W_CLAIM_TYPE` and `W_TWO_PASS` into the linear scoring combiner. `query_claim_type` is computed once per recall when `W_CLAIM_TYPE > 0`. The two-pass stream is `_two_pass_enabled()`-gated and runs between the verbatim and graph streams. Both default to bit-identical legacy ranking.
- `memorymaster/entity_extractor.py` — module docstring + `__all__` list updated to mention the new Layer-1 `library_name` extraction.

### Notes

- All new behaviour is opt-in via env vars and defaults to the legacy code path. No schema migrations applied automatically (run `rebuild_edges` / `rebuild_closets` explicitly to populate the new tables).
- The recall@5 lift hypothesis from F1/F5/F6/F8 against the v3.6.0 N=953 baseline is the next-step measurement (deferred — v3.9.0 ships the machinery + tests, the eval is a v3.10 deliverable so this release stays disciplined).
- The 2 STRICT items from F9 (verbatim-import warning, claim_edges-missing warning) are scoped to v3.9.1.
- Pure additive — no breaking changes, no schema migrations, no API surface removal. Any v3.6.x install upgrades transparently.

## [3.6.0] - 2026-04-27

Honest-null release. Spent significant compute on a tighter L2 prompt + a 9.5×-bigger eval set to definitively answer whether the v3.5.x recall stack has untapped headroom. Answer: **no, the W_LEXICAL/W_FRESHNESS/W_GRAPH weights are at-or-near optimal on every measurable axis, and the GRAPH stream contributes zero across all tested weights**. Shipping the negative result + the new eval/tooling so the next release can attack the actual bottleneck (graph hops formula or labeled-GT bias) instead of re-tuning weights.

### Added

- **L2 entity extraction prompt v3** (`memorymaster/entity_extractor.py::LLM_PROMPT`, version `entity-l2-v3-2026-04-27`): tightened from v2 with `_LLM_MAX_ENTITIES` reduced 8→5, explicit `ALWAYS SKIP` block (file paths, env vars, hostnames, IPs, ports, commit SHAs, branch names, generic tools, generic words like `system`/`config`/`service`/`module`/`sistema`/`proceso`, absolute YYYY-MM-DD dates, code identifiers), per-kind quality bar (e.g. `person_name` requires ≥2 capitalized words AND a real person — not a role like "user"; `concept` requires a named noun-phrase 3+ words usually), and 2 negative examples (bloat + path/SHA noise) showing the empty array as the right answer when nothing rises to the bar. Smoke test on 5 real claims via `claude_cli`: 0.4 entities/claim avg vs the worst v2 batches at 5-7/claim.
- **Hardened `parse_json_response`** (`memorymaster/llm_provider.py`): now resilient to four common LLM output shapes — raw JSON, fenced from start, prose preamble + fenced, prose preamble + raw. Strategy: try direct, then strict-fenced, then regex-extract any fenced block, then greedy first-`[` to last-`]`. Until v3.6.0 only the first two shapes parsed; the others returned `[]` silently. Shipped with `_coerce_to_list` helper. All 30 existing `tests/test_llm_provider.py` + `tests/test_entity_extractor_llm.py` tests still pass.
- **Synthetic prompt set N=953** (`artifacts/real-prompts-1000.jsonl` + `artifacts/real-prompts-1000-labels.json`, 953 deduped from 1000 generated): produced via 5 parallel haiku subagents, mix Spanish + English, 7 categories (architecture / debugging / decision-recall / project-lookup / gotcha-recall / env-config / feature-spec). Labels generated via LLM-judge (10 chunks × 100 prompts via 5 parallel haiku subagents over pre-computed top-15 candidates) — 248/953 prompts have non-empty labels.
- **Pre-compute candidates harness** (`scripts/precompute_candidates.py`): runs production `recall()` once per prompt and writes chunked JSON files for parallel labeling. ~3 min wall for 953 prompts (recall is ~17 ms/query) vs ~5 h serial for the full label-via-LLM path.
- **Label-via-judge harness** (`scripts/label_prompts_with_judge.py`): single-process Python labeler kept for small runs / debugging. Uses claude_cli with direct `os.environ[KEY] = ...` assignment (avoids the v3.5.0 setdefault bug).
- **Roadmap doc** (`artifacts/roadmap-v3.6.0-2026-04-27.md`) + outcome reports (`artifacts/recall-eval-baseline-N953-2026-04-27.jsonl`, `artifacts/recall-weight-tuning-N953-2026-04-27.md`) capturing the negative-result plan and decisions.

### Confirmed (from autoresearch B1, now with N=953)

- **W_LEXICAL alone peaks at 0.106 precision@5** (W=0.05-0.1). Default `W_LEXICAL=0.3` produces 0.105 — within +0.001 = measurement noise. Above W=0.1, MAP@5 monotonically degrades.
- **W_GRAPH stream is FLAT at precision@5 = 0.103 across all weights W=0.0..1.0.** Same null result as v3.5.2 with N=100, now confirmed with 9.5× more samples. The +8,229 entities from the v3.5.0 backfill and the +103 from v3.6.0's v3-prompt re-extraction did not lift the GRAPH stream by any measurable amount on either eval. The next-lever is the `1/(1+hops)` weighting shape or the labels themselves, not weight tuning or entity coverage.
- **W_FRESHNESS hurts above 0.1.** Default 0.0 stays.

### Notes

- No weight default changes shipped. The empirical optimum is within measurement noise of the current defaults across both eval sets.
- L2 v3 is the new default for ALL future extractions (steward + auto-ingest hooks pick it up automatically via `LLM_PROMPT_VERSION`).
- The N=953 prompt set + labels are now in `artifacts/` for any future autoresearch run; reproducible via the chunked precompute + parallel-label pattern documented in the new scripts.
- Pure additive — no schema, API surface, or breaking behavior changes. Any v3.5.x install upgrades transparently.

## [3.5.2] - 2026-04-26

### Added

- **Tests for `_call_claude_cli` provider** (`tests/test_llm_provider_claude_cli.py`, 11 cases): missing binary, non-zero exit, timeout, OSError, UTF-8 / emoji round-trip, `MEMORYMASTER_CLAUDE_CLI_BIN` override, `MEMORYMASTER_CLAUDE_CLI_TIMEOUT` override, default model, alias registration (both `claude_cli` and `claude-cli`), end-to-end dispatch through `call_llm()`. Mocks `subprocess.run` so tests don't invoke the real CLI.
- **Regression test for the v3.5.0 hook env-assignment fix** (`tests/test_hook_env_isolation.py`, 7 cases): asserts `memorymaster/config_templates/hooks/memorymaster-steward-cycle.py` uses direct `os.environ["KEY"] = ...` assignment (not `setdefault`) for `MEMORYMASTER_LLM_PROVIDER`, `MEMORYMASTER_LLM_MODEL`, `MEMORYMASTER_LLM_FALLBACK_PROVIDER`, `MEMORYMASTER_LLM_FALLBACK_MODEL`, plus an import-discipline check (no third-party deps in the hook).
- **Recall weight grid-search harness** (`scripts/grid_recall_weights.py`): 36-cell sweep of `W_LEXICAL × W_FRESHNESS × W_GRAPH` against `eval_recall_precision_at_5.py`. Auto-enables `MEMORYMASTER_RECALL_FRESHNESS=1` / `MEMORYMASTER_RECALL_GRAPH=1` only when the corresponding weight is non-zero (avoids wasted latency on no-op cells). Writes a markdown report and a per-cell JSONL log under `artifacts/grid-runs/`.
- **Recall weight tuning report** (`artifacts/recall-weight-tuning-2026-04-26.md`): full sweep results, decisions, and next-lever notes. **TL;DR:** the current default `W_LEXICAL=0.3` is within measurement noise of the empirical optimum (`W_LEXICAL=0.1` for +0.002 precision@5). The GRAPH stream is flat across all tested weights even after the v3.5.0 +8,229-entity backfill — likely a `1/(1+hops)` weighting / labeled-GT-bias issue, not a weight-tuning issue.
- **Roadmap doc** (`artifacts/roadmap-v3.5.2-2026-04-26.md`): documents the audit + autoresearch ship plan that produced this release.

### Changed

- **Synced steward hook template to the v3.5.0 wiring** (`memorymaster/config_templates/hooks/memorymaster-steward-cycle.py`): primary provider `claude_cli` (haiku-4.5), fallback `ollama` (gemma4:e4b), all four LLM env vars set via direct assignment. Until this release the deployed hook on the developer machine had been hand-edited but the shipped template still defaulted to `google` via `setdefault` — meaning fresh `memorymaster-setup` installs were getting the broken pre-v3.5.0 wiring. Now `memorymaster-setup` deploys the corrected hook out of the box.

### Notes

- No recall weight default changes shipped. Empirical lift ceiling is +0.002 absolute precision@5 — the existing defaults are at-or-near optimum.
- Pure additive release: no schema changes, no API surface changes, no breaking behavior. Existing v3.5.x installs upgrade transparently.

## [3.5.1] - 2026-04-25

### Changed

- **README slimmed from 890 → 187 lines (-79%)**. The full operator-level depth (hooks, dashboard, steward, dream bridge, wiki engine, entity registry, OpenClaw / GitNexus integration, troubleshooting, performance SLOs, one-prompt agent install) moved to a new `docs/handbook.md`. The README is now a 5-minute tour: what it is, prerequisites, quick start, provider table, MCP server template, backends, dev setup, docs index. Existing in-repo links (INSTALLATION.md, CONTRIBUTING.md, ARCHITECTURE.md, USER_GUIDE.md) are preserved.
- README provider table updated to reflect the new `claude_cli` provider added in v3.5.0.
- Install-verify version reference bumped to `3.5.0 or higher`.

### Added

- **`docs/handbook.md`** — single-file operator handbook, ~500 lines. Indexed table of contents, lifted from the prior README sections plus the wiki engine + Bases reference and the Dream Bridge safety rails section.

### Notes

- No code changes — purely documentation reorganization. Behavior identical to v3.5.0.

## [3.5.0] - 2026-04-25

### Added

- **`claude_cli` LLM provider** (`memorymaster/llm_provider.py::_call_claude_cli`): shells out to the local `claude --print --model <name>` binary to use the user's Claude Code OAuth subscription instead of an API key. Registered as `claude_cli` and `claude-cli` in `_PROVIDERS`. Default model `claude-haiku-4-5-20251001`. Override the binary path with `MEMORYMASTER_CLAUDE_CLI_BIN` and the per-call timeout with `MEMORYMASTER_CLAUDE_CLI_TIMEOUT` (default 120s). Defensive: returns `""` on missing binary, timeout, or non-zero exit so the existing `call_llm()` fallback chain transparently routes to the configured backup provider (typically Ollama).
- **Steward / wiki-absorb hook switched to `claude_cli`**: the deployed `~/.claude/hooks/memorymaster-steward-cycle.py` template now defaults primary LLM to `claude_cli` with Ollama `gemma4:e4b` as fallback. Eliminates the Gemini key rotator (six `~/.memorymaster/gemini-keys.env` keys + 429 dance) for periodic curator paths and removes the SQLite writer-lock contention that local Ollama runs introduced during long backfills.

### Changed

- Hook env wiring uses **direct assignment** (`os.environ["KEY"] = ...`) for `MEMORYMASTER_LLM_PROVIDER` and `MEMORYMASTER_LLM_MODEL` instead of `setdefault(...)`. Reason: `setdefault` is a no-op when the inherited shell env already has the var set, which silently leaves a stale provider routed (e.g. provider stays `google` while model gets swapped to a Claude-name → HTTP 404 from Gemini API for every call). Observed in prior cycle as 50 LLM calls 404'ing before the fallback to Ollama saved the run.
- `llm_provider.py` module docstring updated to list the new provider.

### Notes

- **Use the new provider for batched/cron paths only.** Cold-start adds 3-15s per call (subprocess spawn + CLI startup). Fine for steward (50 claims/6h) and wiki-absorb; **NOT** suitable for latency-sensitive recall hooks.
- **OAuth lifetime**: desktop installs of Claude Code do not expire their OAuth tokens. VM installs expire ~24h and require interactive `claude auth login` to refresh — pair with the Ollama fallback for unattended VM use.
- **Cost / scale validation**: a one-shot L2 entity backfill across 11,711 claims (58 batches × 200, 3 parallel haiku subagents) consumed ~5.5M tokens (~1.5% of weekly Claude budget) and produced +8,229 entities (+49.5%) and +18,266 aliases (+50.3%) in ~30 min wall, with zero writer-lock incidents. The same approach applied to steward + wiki-absorb is well inside the user's existing subscription headroom.
- File-based batch I/O pattern (write `in-batchNN.json`, spawn worker, read `out-batchNN.json`, apply via `resolve_or_create` + `add_alias`) is the recommended sidestep for any future bulk LLM job — it avoids the SQLite writer-lock that blocks the steward when a long-running cursor holds a transaction during LLM calls.

## [3.4.1] - 2026-04-16

### Fixed

- **Auto-ingest Stop hook silent failure — CRITICAL**: `_run_gemini_extraction()` in `memorymaster/config_templates/hooks/memorymaster-auto-ingest.py` checked `entry.get("role")` at the top level, but Claude Code transcripts wrap role+content inside `entry["message"]`. The condition evaluated `None != "assistant"` for every line, so the message-extraction loop always produced zero messages and the function silently returned before calling the LLM. Net effect: **every `memorymaster-setup` install since the transcript schema changed had the auto-ingest hook extracting zero learnings per session**. The DB grew only via manual MCP `ingest_claim` calls, recall hook queries, and the classify hook — the promised "Gemini Flash Lite reads the transcript and extracts learnings" pipeline was dead. Fix: normalize with `msg = entry.get("message") if isinstance(entry.get("message"), dict) else entry` before reading role/content, accepting both wrapped and flat shapes. Same fix applied to the deployed hook at `~/.claude/hooks/memorymaster-auto-ingest.py` on the developer machine.

### Added

- **Regression tests** (`tests/test_auto_ingest_hook_schema.py`, 8 tests): parses wrapped, flat, and mixed-schema transcripts; verifies short-message filtering, empty transcripts, and malformed-line skipping. Includes two static checks that fail CI if either the deployed hook or the repo template drops the `entry.get("message")` adapter in a future edit.

### Notes

- Existing users running `memorymaster-setup` on or before 2026-04-16 should re-run `memorymaster-setup` (or upgrade in place) to pick up the fixed hook template. Alternatively, edit `~/.claude/hooks/memorymaster-auto-ingest.py` directly and replace the `entry.get("role")` check with `msg = entry.get("message") if isinstance(entry.get("message"), dict) else entry; if msg.get("role") != "assistant": continue`.
- 7 pre-existing test failures in `test_dedup.py` / `test_sqlite_core.py` tracked separately — Google deprecated `text-embedding-004` endpoint and those tests hit the live API. Unrelated to this fix.

## [3.4.0] - 2026-04-13

### Added

- **Bidirectional claim↔wiki binding**: new `claims.wiki_article` column + index stamps the slug of the wiki article each claim was absorbed into. Closes the one-way link that existed before — wiki frontmatter listed `claims: [ids]` but claims couldn't point back. `wiki_engine.absorb()` now writes both directions in the same pass via the new `_stamp_wiki_binding(db_path, claim_ids, slug)` helper.
- **Recall hook shows the wiki pointer**: `context_hook.recall()` appends `(compiled in [[<slug>]])` next to any claim that has a `wiki_article` stamp, so agents see not just the fact but where its compiled-truth version lives. Inspired by Marcosomma's "Memory Bundle" pattern (binding > recall).
- **New CLI `wiki-backfill-bindings`**: one-shot migration that reads `claims: [ids]` frontmatter from every `<wiki_dir>/**/*.md` and stamps each listed claim with the file's slug. Run once after upgrading to v3.4 to backfill existing vaults.
- **`Claim.wiki_article` field** on the dataclass (default `None`) + readers on both SQLite (`_row_to_claim`) and Postgres (`PostgresStore._row_to_claim`).
- **Tests**: 8 new tests in `tests/test_wiki_binding.py` — schema shape, index presence, idempotent migration, stamp helper, silent no-op on empty input, dataclass roundtrip, recall formatter, backfill handler. Suite is 998 passed / 39 skipped.
- **LLM provider A/B benchmark harness** (`scripts/llm_benchmark.py`): 2-arm comparison (Gemini Flash Lite vs Ollama Gemma 4 e4b with thinking) on real session transcripts. Mirrors the auto-ingest curator prompt. Not part of the runtime; used to validate LLM choices before swaps.

### Changed

- Schema files `schema.sql` and `schema_postgres.sql` include `wiki_article TEXT` on the base `claims` DDL for fresh installs. Existing DBs get the column via the idempotent migration (`_ensure_binding_columns` on SQLite, `_ensure_binding_schema` on Postgres).

### Notes

- The feature is additive and the column is nullable. Pre-v3.4 DBs continue to work; `wiki_article` stays `NULL` until the next `wiki-absorb` run (or until `wiki-backfill-bindings` is invoked).
- Decision trail: benchmark of Gemini Flash Lite vs Gemma 4 e4b (8 sessions) showed Flash Lite extracts 3 claims/session vs Gemma 1 claim/session at the same warm latency (~2.7s). Auto-ingest hook stays on Flash Lite; Gemma remains a candidate for single-output batch tasks (conflict resolver, RESOLVER fallback, wiki-cleanup). `gemma-4-31b-it` via Gemini API (free tier, ~1500/day) works but latency is 10x Flash Lite and thinking can't be disabled — viable only for non-interactive batch.

## [3.3.1] - 2026-04-11

### Fixed

- **Scope hash-suffix bug in `_project_scope()` (mcp_server.py)**: The MCP server was appending a truncated SHA1 digest of the workspace path to every project scope (`project:wezbridge:a6a83c6a`). CLI ingests wrote `project:wezbridge` without the hash, and the two scopes never merged — sessions querying `project:wezbridge` missed claims stored in `project:wezbridge:a6a83c6a` and vice-versa. Fix: default to the canonical `project:<slug>` form. The hash-suffix escape hatch is preserved behind `MEMORYMASTER_SCOPE_DISAMBIGUATE=1` for hosts that genuinely have two workspaces with the same slug. Existing claims with hash suffixes were migrated to the canonical scope (341 claims across 6 scopes).
- **Claim type case inconsistency**: `service.ingest()` now normalizes `claim_type` to lowercase so that routing hints like `DECISION` from the classify hook don't create a duplicate type next to `decision`. 30 existing claims with ALL-CAPS types (GOTCHA, CONSTRAINT, ARCHITECTURE, BUG_ROOT_CAUSE, DECISION, REFERENCE) were normalized.
- **Orphan conflicted claims**: 6 claims had status `conflicted` but their canonical sibling already existed with status `confirmed`. The auto-resolver had skipped them because the confirmed sibling already "won" without competition. They were re-labeled `superseded` with `replaced_by_claim_id` pointing to the winning sibling. Total conflict count: 0.
- **Stale candidates**: Ran a full steward cycle — 200 claims decayed, 195 moved to stale, candidates older than 24h processed.

### Changed

- `project:*:<8hex>` scopes are now migration-compatible — on a fresh DB the scope has no hash, on an old DB the hash is preserved unless a migration strips it. This is the second time this bug has surfaced (first was in v3.2.2 with the tenant_id leak); documented as a constraint claim for future sessions.

## [3.3.0] - 2026-04-10

### Added

- **Entity Registry** (`entity_registry.py`): Canonical entities with alias resolution, inspired by GBrain. New tables `entities` and `entity_aliases` provide identity resolution so "MemoryMaster", "memorymaster", "MEMORYMASTER" all resolve to the same entity. `claim.entity_id` FK links claims to canonical entities. Auto-resolved on ingest via `resolve_or_create()`. CLI commands: `entity-list`, `entity-merge`, `entity-aliases`, `entity-backfill`. 684 existing subjects backfilled in 23ms.
- **RESOLVER.md**: MECE decision tree for wiki article routing (`obsidian-vault/wiki/RESOLVER.md`). 10 canonical types (bug, gotcha, decision, constraint, architecture, environment, reference, entity, pattern, fact) with disambiguation rules. Agents must read this before creating wiki content. Maps directly to the classify hook's routing hints.
- **9 new relationship types**: `implements`, `configures`, `depends_on`, `deployed_on`, `owned_by`, `tested_by`, `documents`, `blocks`, `enables` — expanding `CLAIM_LINK_TYPES` from 5 to 14. Schema migration recreates `claim_links` table with expanded CHECK constraint while preserving existing data. Enables domain-specific graph traversals like "what depends on Qdrant?"
- **`traverse_relationships()`**: BFS graph traversal on claim_links. Accepts `link_types` filter, `max_depth`, and `direction` (outgoing/incoming/both). Returns claims with depth, path, and link_type. Turns the flat claims DB into a queryable knowledge graph.
- **graphify integration**: `pip install graphifyy` + `graphify install` adds the graphify skill to Claude Code for building knowledge graphs from any folder. Not integrated into MemoryMaster codebase — used as a complementary standalone tool.

## [3.2.2] - 2026-04-10

### Fixed

- **5 NameError bugs from cli refactor**: `_score_str_from_payload`, `CitationInput`, `_SCORE_KEYS`, `print_claim` were referenced but not imported in the split handler files. All 5 cause NameError on `history`, `extract-claims --ingest`, `federated-query` CLI commands. Regression tests added for all 4 broken handlers.
- **TypeError on `ghost-notes --json`**: `_handle_ghost_notes` called `_json_envelope()` without the required `query_ms` kwarg.
- **UTF-8 BOM in `metrics_exporter.py`**: broke radon and mypy. Stripped.
- **test_stealth_mode collection error**: `STEALTH_DB_NAME` was auto-removed by ruff F401 fix from cli.py but tests imported it from there. Re-exported with `# noqa: F401` annotation.
- **Sensitivity filter: private IPs removed from canonical ingest filter**: `private_ipv4` pattern was incorrectly blocking legitimate infrastructure claims (e.g. "Server IP is 10.0.0.1"). Private IPs are now only filtered at export time (dream_bridge `_DREAM_EXTRA_PATTERNS`), not at ingest time.

### Added

- **Sensitivity filter extended**: 6 new patterns in `security.py` — Google API keys (`AIza*`), AWS STS keys (`ASIA*`), Slack tokens (`xoxb/xoxp/xoxa`), extended GitHub tokens (`ghu_/ghs_/ghr_`), Telegram bot tokens, DB connection URLs with embedded passwords (`postgres://user:pass@host`). All patterns tested with 20 new security test cases.
- **Sensitivity filter consolidated**: Deleted 4 duplicated regex blocks in `mcp_server.py`, `dream_bridge.py`, `transcript_miner.py`, `verbatim_store.py`. All now call `memorymaster.security.redact_text()` as single source of truth. New public API: `memorymaster.security.redact_text(text) -> (redacted, findings)`.
- **7 regression tests** (`test_handler_regressions.py`) covering all 4 handlers that had F821/TypeError bugs.
- **`autoresearch_daemon.py`**: `git_commit` and `git_revert` now use `run_argv()` (list form, `shell=False`) instead of f-string interpolation into `run()` (`shell=True`), removing a potential command injection footgun.

### Changed

- **130 unused imports cleaned** across 10 files after the cli/storage refactor (ruff F401 autofix).
- **README stats updated**: 22 MCP tools (was 21, `search_verbatim` was undocumented), 64 CLI commands (was "54+"), 1034 tests across 68 modules (was "932 across 66").

## [3.2.1] - 2026-04-10

### Added

- **`memorymaster-setup` entry point**: New `[project.scripts]` entry so pip-installed users can run the interactive installer via `memorymaster-setup` without needing the repo cloned. `scripts/setup-hooks.py` is now a 3-line shim that calls `memorymaster.setup_hooks:main` for backward compat with clone-based workflows.
- **`memorymaster-precompact.py` hook template**: Previously missing from `config-templates/`, now shipped inside the package. Closes the gap where README + CHANGELOG advertised a 7-hook stack but `setup-hooks.py` only installed 6.
- **Package data**: `memorymaster/config_templates/hooks/*.py` and `memorymaster/config_templates/*.md` are now included in the wheel via `[tool.setuptools.package-data]`. `setup_hooks.py` locates templates via `importlib.resources.files("memorymaster")` so it works from both wheel and editable installs.

### Fixed

- **Delete phantom `dict[str` file** from repo root (0-byte file tracked since commit `1d1c33c` via a shell parsing accident).
- **Relax `quick` SLO thresholds** in `benchmarks/slo_targets.json` to survive GitHub Actions runner variance. Observed up to 10x p95 swings between consecutive runs on the same commit (query_p95: 0.053s vs 0.512s, throughput: 19.5 vs 9.9 ops/s). The old thresholds were calibrated against a single lucky run and made CI flaky. New ceilings provide ~20% headroom over the worst observed value; a `_comment` field in the JSON documents the rationale.
- **Align docs with CI install set**: `INSTALLATION.md` troubleshooting previously told users to install `.[dev,mcp,security,embeddings,qdrant]` while CI runs `.[dev,mcp,security]`. The minimal set is the canonical reproduction environment (optional extras skip automatically via `pytest.importorskip`). Docs now match CI.

### Changed

- **Templates moved from `config-templates/` to `memorymaster/config_templates/`**: Required for wheel distribution. `scripts/setup-hooks.py` becomes a shim. README and INSTALLATION.md now document both the `memorymaster-setup` flow (recommended for pip-installed users) and the clone workflow.
- **README + INSTALLATION.md**: Document the 7-hook stack, the `memorymaster-setup` entry point, and the fact that CI uses `.[dev,mcp,security]`.

## [3.2.0] - 2026-04-09

### Added

- **Wiki frontmatter schema**: Every absorbed article in `obsidian-vault/wiki/**/*.md` now carries `description` (~150 char), `tags`, and `date` fields for progressive disclosure. Helpers `_extract_description`, `_build_tags`, `_yaml_escape` in `wiki_engine.py`.
- **Obsidian Bases generator** (`vault_bases.py`): Auto-generates 5 dynamic dashboards (`all-claims.base`, `gotchas.base`, `decisions.base`, `recent.base`, `needs-review.base`) under `obsidian-vault/bases/`. New `bases-generate` CLI command. `wiki-absorb` regenerates Bases automatically (suppress with `--no-bases`).
- **Classify hook** (`config-templates/hooks/memorymaster-classify.py`): Regex signal matcher for UserPromptSubmit with 7 signals (DECISION, BUG_ROOT_CAUSE, GOTCHA, CONSTRAINT, ARCHITECTURE, ENVIRONMENT, REFERENCE) in Spanish + English. Latin-letter lookarounds make it CJK-safe. Zero LLM, ~5 ms runtime.
- **Validate-wiki hook** (`config-templates/hooks/memorymaster-validate-wiki.py`): PostToolUse Edit/Write hook scoped to `obsidian-vault/wiki/**/*.md`. Checks frontmatter completeness and warns on orphan articles (no `[[wikilinks]]` and body > 300 chars).
- **SessionStart hook** (`config-templates/hooks/memorymaster-session-start.py`): Injects recent claims, last cycle summary (ingest/validate/decay/supersession counts), pending candidates, and recently updated wiki articles at session start. Scope auto-derived from cwd.
- **PyPI publish workflow** (`.github/workflows/publish.yml`): Auto-publishes on `git tag v*.*.*` push using PyPI Trusted Publisher with OIDC (no API tokens in secrets).
- **32 E2E tests** (`tests/test_obsidian_mind_patterns.py`) covering all 5 obsidian-mind-inspired components.
- **`benchmarks/README.md`**: Download instructions for the LongMemEval oracle dataset (~15 MB).
- **CLI command**: `bases-generate --output <vault>` regenerates Obsidian Bases on demand.
- **`setup-hooks.py` updates**: Now installs the 3 new hooks alongside the legacy recall + auto-ingest pair.

### Fixed

- **`_seek_to_offset` returned `start_offset = 0` always**: When `MemoryOperator._run_stream_json` resumed from a saved offset, the seek succeeded but the function still returned `(0, read_offset)`, breaking checkpoint resumption. Now returns `(read_offset, read_offset)` on success and `(0, 0)` on error. Fixes `test_run_stream_resumes_from_checkpoint_state` (was the last known flaky test).
- **`test_returns_valid_sha`**: GitHub Actions runners have no global git identity, so `git commit --allow-empty` failed with exit 128. Test now sets `user.email`/`user.name` locally in the temp dir before the commit.
- **`test_semantic_model_calls_transformer`**: Used `import numpy` unconditionally despite numpy not being a base dependency. Now uses `pytest.importorskip("numpy")` so the test skips gracefully when numpy is unavailable.
- **CI: 3 tests failing for 5 consecutive runs** — all 3 fixed above. CI is now green again.

### Changed

- **CLAUDE.md (global)**: Documented SessionStart, classify, and validate-wiki hooks under "How memory flows automatically" so future Claude sessions know to trust the routing hints and react to wiki hygiene warnings.
- **AGENTS.md**: Added wiki frontmatter schema enforcement to Boundaries section.
- **README.md**: Added 3 new entries to Key Features (LLM Wiki, Obsidian Bases, 7-Hook Stack) and a new "New in v3.2" section documenting all the obsidian-mind-inspired patterns.

### Removed

- **Repo cruft from root**: Deleted `entity_extraction.log`, `qdrant_sync.log`, `qdrant_sync_result.json`, `test_output.txt` from the working tree and added them to `.gitignore`.
- **`benchmarks/longmemeval_oracle.json` (~15 MB)** removed from tracking and added to `.gitignore` — it is a public dataset and should be downloaded with the documented commands instead of bloating the repo.

## [3.1.0] - 2026-04-08 (never published to PyPI)

### Added

- **LLM Wiki architecture**: Compiled truth + append-only timeline articles, Karpathy/Farza style. New modules `wiki_engine.py`, `vault_linter.py`, `vault_log.py`, `vault_synthesis.py`, `vault_query_capture.py`.
- **CLI commands**: `wiki-absorb`, `wiki-cleanup`, `wiki-breakdown`, `lint-vault`, `mine-transcript`, `verify-claims`.
- **Verify-claims**: Cross-checks claims that mention file paths or symbols against the actual codebase using `ripgrep`, sub-100 ms per check.
- **MemPalace-inspired upgrades**: Block-based Stop hook with `decision: block` checkpoint every N human messages, PreCompact hook, content-hash dedup (`hash-<sha256>` idempotency keys), bi-temporal `valid_from`/`valid_until` fields on claims, transcript miner.
- **Multi-provider LLM client** (`llm_provider.py`): Google / OpenAI / Anthropic / Ollama with key rotation.
- **Setup script** (`scripts/setup-hooks.py`): Interactive installer for hooks, MCP, env vars, and steward cron.
- **Config templates** (`config-templates/`): Hook templates with `__MEMORYMASTER_PROJECT_ROOT__` placeholder and CLAUDE.md / AGENTS.md append snippets.

### Fixed

- **WAL mode mandatory**: `PRAGMA journal_mode = WAL` now enforced on every connection to prevent DB corruption from concurrent writes (caused by OpenClaw `scp` overwriting an open DB).
- **Hardcoded path in `claim_verifier.py`**: Replaced with dynamic project root detection.
- **35+ silent `except: pass` blocks** in `llm_provider.py`: Now log the exception so API failures are visible instead of returning empty results.
- **Dream-bridge cross-project pollution**: Added scope filter so dream-seed only exports claims from the current project.
- **Hardened sensitivity filter**: Added regexes for Telegram bot tokens, Stripe keys, Supabase keys, and SSH commands.
- **MCP `ingest_claim`**: Auto-generates `CitationInput(source="mcp-session")` when caller does not provide one (was rejecting otherwise-valid ingests with "At least one citation required").
- **Timezone-aware vs naive datetime crash** in `decay.py::_parse_iso`.

## [3.0.0] - 2026-04-05 (never published to PyPI)

### Added

- **Verbatim memory layer** (`verbatim_store.py`): Raw conversation storage table with FTS5 search and Qdrant vector search using OpenAI text-embedding-3-small (1536 dims, Cosine).
- **LongMemEval benchmarks**: `benchmarks/longmemeval_runner.py` (FTS5 baseline, scored 5.6%) and `benchmarks/longmemeval_vector_runner.py` (Qdrant vector, scored 25% on 20 questions). Reference: MemPalace ChromaDB scores 96.6%.
- **Curate-vault command**: LLM-organized Obsidian export with topic clustering and wikilinks (later deprecated by `wiki-absorb`).

## [2.0.0] - 2026-03-08

### Added

- **Centralized Config** (`config.py`): Frozen `Config` dataclass with 11 env vars + JSON config file support. All hardcoded weights replaced with configurable values.
- **Context Optimizer** (`context_optimizer.py`): `query_for_context(budget=4000)` with greedy knapsack packing and 3 output formats (text/xml/json). New `query_for_context` MCP tool (13 total).
- **Conflict Resolution** (`conflict_resolver.py`): 5-tier auto-resolution (pinned > confidence > recency > citations > id), `contradicts` links, and `policy_decision` audit events.
- **Deduplication** (`jobs/dedup.py`): Two-gate detection (cosine similarity + text overlap), chain prevention, `supersedes` links, summary events.
- **Staleness Detection** (`jobs/staleness.py`): File watcher with `mtime` and `git` modes, citation-based path extraction, pinned claim exclusion.
- **LLM Compaction** (`jobs/compact_summaries.py`): Embedding-based clustering with LLM summarization, `derived_from` links, confirmed summary claims.
- **Git Versioning** (`snapshot.py`): SQLite `.backup()` API snapshots, rollback with safety backup, field-level diff, post-commit hook installer.
- **Claim Graph**: `claim_links` table with 5 typed relationships (`supersedes`, `contradicts`, `supports`, `derived_from`, `relates_to`).
- **Hierarchical IDs**: `mm-{4hex}.{n}.{n}` human-readable IDs derived from `derived_from` links, accepted in all CLI commands.
- **Multi-tenancy**: Row-level `tenant_id` isolation at service layer with `_check_tenant_access()` enforcement.
- **Connection Retry** (`retry.py`): Exponential backoff wrapper for SQLite and Postgres connections.
- **Operator Queue** (`operator_queue.py`): SQLite WAL-backed FIFO with atomic dequeue and crash recovery.
- **Key Rotation**: Round-robin API key selection with per-key cooldown tracking on 429 errors.
- **Auto-validate Pipeline**: Chained extraction + deterministic validation after LLM claim extraction.
- **FTS5 Search**: Content-synced FTS5 virtual table with BM25 ranking and proper query escaping.
- **Semantic Embeddings**: 3-tier fallback (sentence-transformers MiniLM-L6-v2, Gemini API, hash-v1) with `is_semantic` weight switching.
- **JSON Output**: Global `--json` flag for all CLI commands with structured envelope format.
- **Stealth Mode**: `--stealth` flag for local-only experimentation with auto-detection.
- **New CLI Commands**: `context`, `dedup`, `resolve-conflicts`, `ready`, `history`, `link`/`unlink`/`links`, `check-staleness`, `compact-summaries`, `snapshot`/`snapshots`/`rollback`/`diff`, `install-hook`, `stealth-status`.
- **Postgres Parity**: 32/32 public method parity with SQLite store including claim links, human IDs, and tenant filtering.
- **380+ tests** across 40+ test modules (up from 82 tests in v1.0.0).

### Fixed

- Dashboard test assertions updated to match actual HTML output (`">Claims<"` instead of `"Claims Table"`).
- Steward `_get_git_head()` hardened with timeout, path resolution, and 40-hex output validation.
- Scheduler `get_git_head()` hardened with same protections.
- `_is_valid_url()` now validates hostname via IP address or regex (was accepting malformed URLs).
- Decay module now uses `DECAY_BY_VOLATILITY` constant instead of missing reference.
- Bearer token redaction pattern lowered minimum from 20 to 8 chars to catch short tokens.
- Added JWT, GitHub token, hex token, markdown credential, inline credential, and connection string redaction patterns.

### Changed

- Version bump from 1.1.0 to 2.0.0 (major: new public API surface, multi-tenancy, claim graph).
- Retrieval weights switch automatically based on `is_semantic` embedding provider.
- All hardcoded weights across 5 modules replaced with `get_config()` lookups.
- Service layer now uses `create_best_provider()` for automatic embedding tier selection.
- Added `embeddings` and `gemini` optional dependency groups to `pyproject.toml`.

## [1.0.0] - 2026-03-07

### Added

- **Core Engine**: 6-state claim lifecycle (`candidate` -> `confirmed` -> `stale` -> `superseded` -> `conflicted` -> `archived`) with append-only event log and citation tracking.
- **Structured Claims**: Subject-predicate-object triples with confidence scores, volatility tags, and scope isolation.
- **Hybrid Retrieval**: Lexical + vector + freshness + confidence ranking with progressive tiered fallback.
- **Steward Governance**: Filesystem grep, deterministic format, citation locator, semantic probe, and tool probe validators with human-in-the-loop proposal/approve/reject workflow.
- **Operator Runtime**: JSONL inbox streaming with restart-safe checkpointing, durable pending-turn queue, progressive retrieval, and configurable maintenance cadence.
- **MCP Server**: 12 tools for Claude Code / Codex integration (`init_db`, `ingest_claim`, `run_cycle`, `query_memory`, `list_claims`, `list_events`, `pin_claim`, `compact_memory`, `run_steward`, `list_steward_proposals`, `resolve_steward_proposal`, `open_dashboard`).
- **Dashboard**: Real-time HTML dashboard with claims table, timeline feed, conflict comparisons, review queue, and SSE operator stream.
- **Connectors**: Import from Git commits, tickets, Slack, email (IMAP), Jira, GitHub, and generic OpenAI/Claude/Gemini conversation exports.
- **Security**: Auto-redaction of tokens/keys/passwords at ingest, policy-gated sensitive access, Fernet encryption for raw payloads, and non-destructive `redact-claim` with audit trail.
- **Dual Backend**: Full SQLite and Postgres (with optional pgvector) parity.
- **Performance**: SLO-driven benchmarks with configurable profiles (`quick`, `sustained`, `production`), p95 latency gates, throughput floors, and zero-miss quality checks.
- **Incident Drills**: Automated drill runner with perf + eval + operator E2E + integrity reconciliation + compaction traceability + HMAC-signed signoff artifacts.
- **Metrics Export**: Prometheus text format and structured JSON metrics from operator event logs.
- **Review Queue**: Priority-ranked triage of stale/conflicted claims with dashboard approve/reject actions.
- **Compaction**: Citation-preserving history summarization with traceability graph artifacts.
- **82 tests passing** across 21 test modules covering core, steward, operator, dashboard, connectors, and performance.

### Fixed

- SSE stream newline encoding (was sending literal `\n` instead of actual newlines).
- Operator JSON decode error handling (was blocking queue permanently instead of skipping bad entries).
- Operator event naming (`json_error` consistent with dashboard SSE listener).
- Review queue sensitive claim filtering (now properly passes `allow_sensitive` through to `list_claims`).
- Python 3.12 compatibility for `@dataclass(slots=True)` with `importlib.util` module loading.
- Steward test helpers now bypass SQLite uniqueness guards correctly.

## [0.1.0] - 2026-02-15

### Added

- Initial prototype with SQLite backend, basic ingest/query/cycle, and CLI.
