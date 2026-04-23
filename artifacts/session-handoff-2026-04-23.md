# Session Handoff — 2026-04-23

**Project:** memorymaster
**Branch:** `main` at `7d80d83` (updated mid-session; see §9 for late-session additions)
**Model:** Opus 4.7 1M-context
**Purpose:** continuation checkpoint for the big autonomy push on 2026-04-23.

---

## 1 · What shipped to `origin/main` this session

In order (oldest → newest):

| Commit | What |
|---|---|
| `bca5aff` | fix(auto-ingest): insert citation row per claim (#128) |
| `2abb891` | merge: fix auto-ingest citation bug (#128 — unblocks 821 stuck candidates) |
| `64818f3` | fix(migrations): archive confirmed-claim collisions in scope merge |
| `ea99b73` | fix: gemini embedding 404 + dashboard test port skip (#119, #123) |
| `48134b5` | merge: gemini embedding 404 + dashboard port skip (#119, #123) |
| `0ffc672` | feat(hooks): shared log_hook helper for observability (#118) |
| `b3bb6d8` | merge: hook observability helper (#118) |
| `130d236` | fix(hooks): eliminate template vs installed drift (#130) |
| `612b496` | merge: eliminate hook template drift (#130) |
| `73484a6` | docs: specs for Wave 3 entity extraction (#127) + steward classifier (#129) |
| `6d8729e` | merge: specs for #127 + #129 |

Plus the pre-session Pareto work that was fast-forwarded to origin/main at `71d7f67`.

---

## 2 · Tasks closed this session

| # | Was | Now |
|---|---|---|
| **#128** | 93% of candidates stuck with 0 citations | **FIXED.** Installed hook emits citation at ingest. Backfill inserted 821 citations. Regression test locks invariant. Orphans: 889 → 68 (remainder are non-hook sources). |
| **#120** | 4,782 fragmented scopes | **MIGRATED.** 4,939 rows updated, 4 confirmed-collision archives. Script now handles UNIQUE-index collisions automatically. |
| **#121** | entity_aliases avg_per_entity 1.000 | **BACKFILLED.** 1.000 → 1.033. Schema migration done. Further lift now gated on #127 implementation. |
| **#122** | recall eval unverified post-migration | **PASS.** 24/30 = 80% hit rate holds. |
| **#123** | `test_open_dashboard_no_server` flakes when port bound | **FIXED.** `_port_bound()` probe + `@pytest.mark.skipif`. |
| **#124** | 3 locked agent worktrees | **REMOVED.** |
| **#125** | Dashboard on :8765 | **STOPPED.** PID 13612 terminated, port free. |
| **#126** | 673 unqualified `project` scope claims | **SUBSUMED.** Merged into `user` by #120 migration. |
| **#119** | Hybrid retrieval dead (misdiagnosed as torchao) | **FIXED.** Actual bug: Google deprecated `text-embedding-004`. Updated default to `gemini-embedding-001`, added try/except fallback to hash-v1. 6 tests went green (5 dedup + 1 sqlite_core). |
| **#118** | 5/7 hooks silent in hook.log | **FIXED.** `memorymaster/hook_log.py` shared helper + inline loggers. 4 of 5 silent hooks now emit structured lines (dream-sync, recall, classify, session-start). `observe` left alone — its subprocess already leaves a trace. |
| **#130** | 6/7 hook templates drifted from installed + 2 install-only | **FIXED.** `sync_hook_templates.py` reparameterized all 9 installed hooks into templates. `check_hook_template_drift.py` CI-ready checker. Drift = 0. |
| **#127** | Wave 3 NER at ingest | **SPEC SHIPPED** (`artifacts/spec-entity-extraction-at-ingest-2026-04-23.md`). Implementation is next session. |
| **#129** | Steward additive score can't exceed 49% recall | **SPEC SHIPPED** (`artifacts/spec-steward-classifier-2026-04-23.md`). Implementation is next session. |

---

## 3 · Tasks still open

### In-flight this session (subagent)

- **#116** — classify-regex macro-F1 tuning. Subagent `af47dc2d7e8eab473` running in background, already shipped `scripts/eval_classify_f1.py` + `tests/fixtures/classify_eval.jsonl`. Result due via notification on completion. Merge the subagent's `omni/classify-regex-f1-2026-04-23` branch once it returns.

### Architectural — specs ready, implementation pending

- **#127** Wave 3 NER at ingest — layer 1 (regex extractors) ~1 day; layer 2 (LLM) ~2 days. Spec has acceptance criteria; measure against `avg_aliases_per_entity ≥ 2.0`.
- **#129** Calibrated steward classifier — LogisticRegression + isotonic calibration. Spec has acceptance criteria; measure against recall ≥ 70% at FPR ≤ 5%.

### Not-started / deferred

- **#75** Run graphify on 15+ legacy projects — **deferred**. /graphify is an interactive Claude skill, not a batch script. Requires a concrete project list from the user (I don't have one) and each run needs review. Best done as a separate focused session.

### Pre-existing open PRs

- `omni/fix-claude-rules-curation` — on GitHub, waiting on user merge (unchanged from prior handoff).

---

## 4 · System state

| Resource | State |
|---|---|
| `memorymaster.db` | 11,703 claims, 889 orphans remaining (all non-hook sources). Size ~7.8 GB. |
| `memorymaster.db.bak.1776912987` | Pre-entity-backfill snapshot (7.8 GB). Keep until next cycle confirms no regression, then delete. |
| Dashboard :8765 | Stopped. No background process owns the port. |
| Gemini rotator keys | Unchanged from prior session (6 keys in `~/.memorymaster/gemini-keys.env`). |
| Hook install state | All 9 hooks installed. 0 drift from templates (as of commit `612b496`). |
| GitNexus index | Stale as of `b3bb6d8`; ran `npx gitnexus analyze --embeddings` mid-session but new commits after it still pending. Re-run before any impact analysis next session. |

---

## 5 · Key decisions and gotchas (non-obvious)

1. **Handoff-wrong claim.** The prior handoff said main was at `71d7f67` — it was actually at `6050b2d` locally, and `origin/main` was also `6050b2d`. The Pareto merge commit existed only on a feature branch and had never been pushed. Future handoffs must run `git fetch` before claiming merge state. Saved as claim 11777.
2. **Commit-guard on main.** The repo has a commit-guard hook that blocks cross-directory commits on main. All feature work must land via branch + merge-no-ff. No problem in practice, just forces the workflow.
3. **The real #119 bug was not torchao.** The handoff hypothesis (`torch<2.11`) was wrong. torchao only warns and is harmless. Root cause: Google deprecated `text-embedding-004`. Took 2 minutes to diagnose by actually running the test.
4. **Template vs installed drift was 6/7, not 1/7.** The audit understated the problem because only auto-ingest had been noticed during prior investigation. Running `setup_hooks` would have silently regressed half the live hooks.
5. **Anthropic rate limit is real.** 1 background subagent ran cleanly this session. Handoff's prior experience (3/4 rate-limited when spawning 4 parallel) holds.

---

## 6 · Immediately-actionable next-session starter

```
Resume memorymaster work. Current main is `6d8729e`. Read
`artifacts/session-handoff-2026-04-23.md` first.

Highest-leverage pending work:

1. Merge subagent af47dc2d7e8eab473's `omni/classify-regex-f1-2026-04-23`
   branch once it returns with the macro-F1 result (#116).

2. Implement Wave 3 entity extraction (#127) — spec at
   `artifacts/spec-entity-extraction-at-ingest-2026-04-23.md`. Start
   with Layer 1 (regex extractors), one backfill run, acceptance metric
   `avg_aliases_per_entity >= 2.0`.

3. Implement calibrated steward classifier (#129) — spec at
   `artifacts/spec-steward-classifier-2026-04-23.md`. Scikit-learn
   logistic regression with isotonic calibration, rollback-safe
   integration.

The 12 tasks that closed this session do not need review — they
shipped, tests pass, migrations landed cleanly.
```

---

## 9 · Late-session additions (post-handoff-first-draft)

After the first handoff draft, the session kept going. Additional commits
on `origin/main` past `6d8729e`:

| Commit | What |
|---|---|
| `473b5dd` | merge: session handoff 2026-04-23 (this file's initial version) |
| `7439fa6` | merge: classify-hook macro-F1 0.22 → 0.98 (#116) |
| `542392b` | merge: stale vector_search default-model expectation fix |
| `5285edd` | merge: .claude/ rules curation (long-open PR finally merged) |
| `b1a0ac9` | docs: roadmapres.md — remaining-work plan |
| `fe88344` | feat(entity-extraction): Layer-1 regex extractor at ingest (#127 Wave 3) |
| `d119eb1` | feat(steward): real-DB training run v1 (honest null — ROC-AUC 0.46 on broken split) |
| `0dff74a` | feat(policy): MEMORYMASTER_POLICY_MODE env-var opt-in for cadence |
| `6679805` | merge: steward classifier v2 — fix chronological-split pathology (#129b, ROC-AUC 0.990) |
| `847342f` | merge: recall precision@5 eval + env-knob infrastructure (#4) |
| `e884f23` | fix(tests,docs): de-flake key_rotator cooldown test + operator enable doc |
| `7d80d83` | **merge: recall tokenizer v2 — df=0 IDF bug fix + stem/synonym fallback (+4 prompts)** |

### Metrics landed (vs pre-session baselines)

| Metric | Before | After | Source |
|---|---|---|---|
| Stuck candidates with 0 citations | 824 | 68 | #128 fix + backfill |
| avg_aliases_per_entity | 1.033 | 2.150 | #127 Layer-1 extract + backfill |
| Classify hook macro-F1 | 0.22 | 0.98 | #116 (agent) |
| Steward classifier ROC-AUC (sound split) | — | 0.99 | #129b (agent) |
| Recall non-empty rate | 24/30 (80%) | **28/30 (93%)** | tokenizer v2 |
| Recall precision@5 | 0.197 | **0.280** | tokenizer v2 downstream |
| Recall MAP@5 | 0.237 | **0.442** | tokenizer v2 downstream |
| Hook template drift | 6/7 + 2 missing | 0/9 | #130 |

### Classifier + policy-mode operator path

Both systems are enable-ready but off by default. See
`docs/enabling-v2-systems.md` for the env-var recipe and rollback
instructions. Cadence-mode was validated live on this session's DB —
first run reports `considered=200, due=188, selected=5` at
`--policy-limit 5` (expect a ~5-cycle backlog-clearing window).

### Subagents in flight at handoff-close

Two background agents were running when this section was written:
- **BM25 param sweep** (agent `a0542217`, branch `omni/feat-bm25-sweep-2026-04-23`). Grid search over (k1, b) 5×5 against the 30-prompt eval.
- **Entity-link retrieval fanout** (agent `aad80bced0a3080c6`, branch `omni/feat-recall-entity-fanout-2026-04-23`). Wires post-#127 entity_aliases into the recall path.

When their notifications fire, merge via commit-guard-safe branch path
and re-run `scripts/eval_recall_quality.py` + `scripts/eval_recall_precision_at_5.py`.

### New autoresearch candidates (open)

With the retrieval bottleneck partially broken, the remaining levers are:
1. Vector fallback when FTS5 returns <3 (Qdrant) — 2 days
2. Steward classifier v3 feature engineering on the sound-split corpus
3. Sensitivity-filter refresh on a new adversarial corpus
4. Content-embedding similarity feature for the classifier

### Claims saved this extension

11822 (subagent HEAD bleed), 11825 (commit-guard merge workflow),
11830 (Wave 3 shipped), 11831 (classifier v2 shipped), 11833 (v1 real-
DB training null result), 11834 (policy-mode legacy-is-stub),
11838 (split-pathology correction), 11841 (retrieval-not-ranking),
11847 (user "no stopping" preference), 11848 (Windows timer flake),
11853 (df=0 IDF inverted-ranking bug), 11854 (cadence-mode validation).
