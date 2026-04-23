# MemoryMaster — Final Roadmap (2026-04-23)

**Main at** `a315bf5` as of writing.
**Scope:** every honest pending improvement + explicit "what's blocked on you" list.
**Purpose:** single source of truth for the next autonomous run. Pasting the prompt at the bottom into a fresh session should drive the list to zero.

---

## 0 · Ground rules (read first)

- Every unchecked item has an **acceptance criterion**, an **agent-ready or user-input-required** flag, and a **risk level**.
- Anything marked **AGENT-READY** can run via `Agent` tool with `isolation=worktree` with no further human input.
- Anything marked **USER-INPUT** is waiting for a decision or an action that only the human can provide.
- Anything marked **DESTRUCTIVE** (deletes files, mutates live DB outside idempotent scripts) needs explicit user approval even inside the autonomous run.
- Per claim 11847: do NOT propose stopping. Queue the next item automatically.
- Per claim 11822: agents that share worktree bleed HEAD into the parent session — always use `isolation=worktree`.
- Per claim 11825: commit-guard blocks merges on main with >3 staged files; always use branch → ff-merge.
- Per claim 11761: ≤3 parallel heavy agents (4+ hit Anthropic rate limit).

---

## 1 · Retrieval improvements

### 1.1 · [ ] RRF (Reciprocal Rank Fusion) as alternative to linear `_relevance`
- **Status:** AGENT-READY
- **Risk:** low (feature-flagged, default off)
- **Acceptance:** p@5 on 30-prompt eval at min_overlap=1 ≥ 0.80 (current 0.793); MAP@5 ≥ 0.86
- **Files:** new `memorymaster/recall_fusion.py`, env var `MEMORYMASTER_RECALL_FUSION=rrf|linear`, wire into `context_hook.py::recall`
- **Estimate:** ~1 day
- **Why:** linear combination of 8 weights is heuristic. RRF is the classical IR multi-retriever fusion; might beat current stack or prove linear is fine.

### 1.2 · [ ] Scope-aware retrieval boost
- **Status:** AGENT-READY
- **Risk:** low (opt-in env var)
- **Acceptance:** current-scope claims rank above cross-scope by ≥0.1 score margin when both retrieved; no p@5 regression
- **Files:** `context_hook.py` — add a `scope_match_bonus` dim or fold into `_relevance` as a conditional multiplier
- **Estimate:** half a day
- **Why:** claims from the active project are almost always more relevant than global; today this is only a retrieval filter, not a ranking signal

### 1.3 · [ ] Expand eval set from 30 → 100 labeled prompts
- **Status:** AGENT-READY (can build programmatically + LLM label with human spot-check sample)
- **Risk:** medium (eval is fuzzy; false-positive labels could misguide tuning)
- **Acceptance:** `artifacts/real-prompts-100.jsonl` with 100 prompts + ground-truth token-set labels; re-run current eval and report baseline
- **Files:** `scripts/expand_recall_eval.py`, `artifacts/real-prompts-100.jsonl`
- **Estimate:** ~1 day (if LLM-labeled) or ~3 hrs (human label 70 new ones)
- **Why:** 30 prompts is noisy; several recent subagents hit "gain is real but below ship threshold" at 30 that might clear at 100

### 1.4 · [ ] BM25 per-field weighting
- **Status:** AGENT-READY
- **Risk:** low
- **Acceptance:** p@5 lift ≥ 0.02 or honest null
- **Files:** `context_hook.py` — BM25 rescorer currently concatenates subject+text; split into weighted streams (subject 2x text)
- **Estimate:** half a day

### 1.5 · [ ] Query expansion via entity-matched synonyms
- **Status:** AGENT-READY
- **Risk:** medium (expansion can dilute precision)
- **Acceptance:** p@5 non-regression with recall non-empty lift ≥1 prompt; evaluated vs current stack
- **Files:** `memorymaster/query_expansion.py`, `context_hook.py`

---

## 2 · Steward / classifier

### 2.1 · [ ] Content-embedding similarity as classifier feature (v3)
- **Status:** AGENT-READY
- **Risk:** low (v3 artifact shipped alongside v2; only activated via env path)
- **Acceptance:** ROC-AUC on held-out chronological split ≥ 0.99 (matching v2); fixes the ~0.45 chronological split failure by making the feature robust to population drift
- **Files:** `memorymaster/steward_features.py` adds `wiki_similarity_cosine` feature; `scripts/train_steward_classifier.py --version v3`; ship `artifacts/steward-classifier-v3.joblib`

### 2.2 · [ ] Backtest v3 against v2 on the same real-DB rolling window
- **Status:** AGENT-READY (uses existing `scripts/backtest_steward_classifier.py`)
- **Acceptance:** artifacts/steward-classifier-v3-backtest-2026-04-24.md with confusion matrices + v2-vs-v3 disagreement samples

### 2.3 · [ ] Enable v2 classifier in prod — OPERATOR
- **Status:** **USER-INPUT** (operator env flip per `docs/enabling-v2-systems.md`)
- **Action:** set `MEMORYMASTER_STEWARD_CLASSIFIER_ENABLED=1` + `MEMORYMASTER_STEWARD_CLASSIFIER_PATH=artifacts/steward-classifier-v2.joblib` in hook env
- **Risk:** changes every promotion decision live

### 2.4 · [ ] Enable cadence policy mode — OPERATOR
- **Status:** **USER-INPUT** (operator env flip)
- **Action:** set `MEMORYMASTER_POLICY_MODE=cadence`
- **Risk:** 188/200 first-cycle backlog; more Gemini LLM calls per steward cycle

---

## 3 · Entity extraction (Wave 3)

### 3.1 · [ ] Layer 2 LLM entity extraction
- **Status:** AGENT-READY but costly (Gemini calls on ingest when env flag on)
- **Risk:** medium (LLM latency + cost)
- **Acceptance:** avg_aliases_per_entity ≥ 2.5 on re-backfill; Layer-2 activation gated by `MEMORYMASTER_ENTITY_LLM=1`; idempotent
- **Files:** `memorymaster/entity_extractor.py` adds `extract_llm()`, `scripts/backfill_entity_extraction.py` adds `--layer2` flag
- **Spec:** `artifacts/spec-entity-extraction-at-ingest-2026-04-23.md`

### 3.2 · [ ] Entity kind expansion (Spanish surnames, time expressions, model names)
- **Status:** AGENT-READY
- **Acceptance:** adds ≥3 new kinds; re-backfill shows avg_aliases ≥ 2.3 (vs current 2.15)

---

## 4 · Sensitivity filter

### 4.1 · [ ] Fresh adversarial corpus + F1 re-measurement
- **Status:** AGENT-READY
- **Risk:** low
- **Acceptance:** `tests/fixtures/sensitivity_adversarial_v2.jsonl` with 100 NEW samples (50 pos secrets, 50 neg secret-adjacent prose); F1 ≥ 0.95 preserved
- **Why:** existing 0.995 F1 might overfit the Wave 1-C corpus

---

## 5 · Instrumentation

### 5.1 · [ ] Retrieval latency counters
- **Status:** AGENT-READY
- **Acceptance:** p50/p99 latency for each recall stream (FTS5, entity fanout, BM25 rescorer, vector fallback, verbatim) logged to `hook.log`
- **Files:** `memorymaster/context_hook.py` — wrap each stream in timer; emit via `log_hook()`

### 5.2 · [ ] Classify hook latency budget test
- **Status:** AGENT-READY
- **Acceptance:** pytest asserts classify hook runtime < 15ms on fixture prompts
- **Files:** `tests/test_classify_hook_latency.py`

---

## 6 · Benchmarks

### 6.1 · [ ] LongMemEval benchmark run against current stack
- **Status:** AGENT-READY (benchmark harness exists per memory-benchmark claims)
- **Acceptance:** `artifacts/longmemeval-2026-04-24.md` with score vs MemPalace 96.6% baseline; hit rate per question type
- **Why:** we have all three streams (BM25, entity, vector, verbatim) — time to measure against the benchmark that motivated them

### 6.2 · [ ] RRF vs linear fusion comparison on LongMemEval
- **Status:** AGENT-READY but depends on 1.1 and 6.1

---

## 7 · Data hygiene

### 7.1 · [ ] DESTRUCTIVE: delete 3 DB backup files (~6.9 GB)
- **Status:** **USER-INPUT** (destructive)
- **Files:** `memorymaster.db.bak.1776912987`, `memorymaster.db.bak.1776949606-pre-entity-backfill`, `memorymaster.db.corrupted`
- **Action:** single-line `rm` when approved

### 7.2 · [ ] Clean up locked `.claude/worktrees/agent-*` directories
- **Status:** AGENT-READY (low risk)
- **Acceptance:** `git worktree list` shows only main; no orphan dirs under `.claude/worktrees/`

### 7.3 · [ ] Fix pre-existing `test_operator::test_run_stream_resumes_from_checkpoint_state` flake
- **Status:** AGENT-READY
- **Acceptance:** 5 consecutive runs in isolation all pass
- **Why:** known-flaky per AGENTS.md; adds noise to every CI run

---

## 8 · Documentation

### 8.1 · [ ] Architecture doc covering the 5-stream recall stack post-2026-04-23
- **Status:** AGENT-READY
- **Acceptance:** `docs/recall-architecture-2026-04-23.md` with diagram + each stream's env gates
- **Why:** 5 retrieval streams + 8-dim ranking is now complex enough to warrant one canonical doc

### 8.2 · [ ] ADR: tokenizer v2 IDF df=0 fix decision
- **Status:** AGENT-READY
- **Acceptance:** `docs/adr/2026-04-23-tokenizer-v2-idf-fix.md` capturing root cause, fix, measured lift

### 8.3 · [ ] ADR: steward v2 classifier — why feature engineering beat threshold tuning
- **Status:** AGENT-READY

---

## 9 · Blocked / deferred

### 9.1 · [ ] #75 Run graphify on 15+ legacy projects — **USER-INPUT**
- Needs concrete project list
- /graphify is interactive, best as dedicated session

### 9.2 · [ ] Wiki absorb freshness metric — **USER-INPUT**
- "What counts as fresh?" is a product decision, not a measurement one
- Can't AGENT-READY without agreement on the metric shape

---

## 10 · Autonomous-run prompt (paste into a fresh session)

Copy-paste from here:

````
Resume MemoryMaster from artifacts/final-roadmap-2026-04-23.md.
Current main is `a315bf5`. Read the roadmap in full before anything else.

Rules that are non-negotiable:
- Per claim 11847: do NOT propose stopping. Queue the next AGENT-READY
  item automatically. Only stop if I explicitly tell you to.
- Per claim 11822: spawn subagents with isolation=worktree only.
- Per claim 11825: commit-guard blocks main when >3 staged files.
  Always branch → push → ff-merge main.
- Per claim 11761: cap parallel agents at 3.
- Never run DESTRUCTIVE or USER-INPUT items autonomously.
- After every merge: save a claim_type='architecture' or 'bug' ingest
  if the finding is non-obvious (claim 11872+).
- After every merge: refresh gitnexus in background.

Execution order (respect dependencies):
  Wave A (parallel ≤3): 1.1 RRF, 1.4 BM25 per-field, 7.2 worktree cleanup
  Wave B (parallel ≤3): 1.3 expand eval, 3.1 L2 LLM extraction, 4.1 sensitivity refresh
  Wave C (parallel ≤3): 2.1 classifier v3, 2.2 v3 backtest, 5.1 latency counters
  Wave D (sequential):  6.1 LongMemEval (depends on Waves A-C results),
                        6.2 RRF vs linear on LongMemEval (depends on 1.1 + 6.1)
  Wave E (parallel ≤3): 8.1 arch doc, 8.2 ADR IDF fix, 8.3 ADR classifier
  Wave F: 7.3 fix test_operator flake (in-session, small)
  Wave G: 1.2 scope-aware boost, 1.5 query expansion, 3.2 new entity kinds

For each agent: give it explicit do-not-touch lists (other waves' files)
and the relevant claim IDs from memory (11847, 11822, 11825, 11761).

At the end:
  1. Extend artifacts/session-handoff-2026-04-23.md §9 with the new commits
     + metrics.
  2. Update artifacts/final-roadmap-2026-04-23.md checkboxes for every
     completed item.
  3. Flag remaining USER-INPUT items explicitly.
  4. Stop ONLY when every AGENT-READY checkbox is ticked.
````

---

## Current session shipped (for context)

Main commits post-starter:
- `bca5aff` fix(auto-ingest): insert citation row per claim (#128)
- `64818f3` fix(migrations): archive confirmed-claim collisions in scope merge
- `ea99b73` fix: gemini embedding 404 + dashboard test port skip
- `b3bb6d8` feat(hooks): shared log_hook helper
- `612b496` fix(hooks): eliminate template vs installed drift
- `73484a6` docs: specs for Wave 3 + steward classifier
- `7439fa6` merge: classify-hook macro-F1 0.22 → 0.98
- `fe88344` feat(entity-extraction): Layer-1 regex extractor
- `d119eb1` feat(steward): real-DB training run v1 honest null
- `0dff74a` feat(policy): MEMORYMASTER_POLICY_MODE env switch
- `6679805` merge: steward classifier v2 sound-split 0.990
- `847342f` merge: recall precision@5 eval + env knobs
- `e884f23` de-flake key_rotator + operator enable doc
- `7d80d83` merge: tokenizer v2 df=0 IDF bug fix (+4 prompts)
- `da2552d` merge: entity-link retrieval fanout
- `159eef7` merge: BM25 rescorer (+40% p@5)
- `274577d` merge: Qdrant vector fallback
- `3f1777c` merge: MemPalace-style verbatim retrieval
- `981bd7b` merge: steward v2 backtest +0.02 F1 real-data
- `a315bf5` feat(recall): W_LEXICAL default 0.1 → 0.3 (+0.125 p@5)

Claims saved (partial): 11775, 11776, 11777, 11811, 11812, 11813, 11821,
11822, 11825, 11830, 11831, 11833, 11834, 11838, 11841, 11847, 11848,
11853, 11854, 11855, 11856, 11857, 11869, 11870, 11871.

_End of roadmap._
