# Campaigns C, D, F, N: retrieval quality, hooks, Jev, cost

This is a static, read-only inspection at HEAD `31c9492` (2026-10-04). Nothing was measured. The only execution was a JSON count on the local LongMemEval file.

## C. Retrieval quality and real usefulness (priority 1)

**Status:** confirmed by code for the metric set and the missing cases. Quality itself is not evaluated.

**Evidence**
- `tests/bench_longmemeval.py` computes only Recall@5, Recall@10 and MRR at session level (`score_retrieval` l.360, `aggregate_retrieval` l.396), with a breakdown by `question_type` (l.926). It has **no nDCG, citation precision or abstention**.
- The dataset `benchmark/data/longmemeval_s_cleaned.json` has 500 questions:

  | Question type | Count |
  |---|---|
  | multi-session | 133 |
  | temporal | 133 |
  | knowledge-update | 78 |
  | single-session-user | 70 |
  | single-session-assistant | 56 |
  | preference | 30 |

  Of these, 30 are `_abs`, meaning the question has no answer. The scorer treats them as ordinary retrieval.
- The project's own qrels, `tests/fixtures/qrels_search.json`, hold 12 queries over 12 claims, all in English. The thresholds are top1 ≥ 0.7 and recall@5 ≥ 0.85.
- There are **no** sets for aliases, negations, temporal changes, unanswerable questions or cross-project questions.
- The gate already enforces a policy (`3e0fcc4`). The QA script is resumable and checks the fingerprint of its inputs.

**Hypothesis:** a labelled Spanish/English set with abstention and knowledge-update cases exposes failures that session-level Recall@k/MRR hide.

**Metrics:**
- Primary: nDCG@10 and Recall@5 per case class.
- Secondary: MRR, citation precision, and the rate of false answers on unanswerable questions.
- Constraints: 0 provider calls, a disposable database, deterministic ranking.

**Side effects:**
- `bench_longmemeval.py` uses a disposable database (l.253).
- It loads the local embeddings model (`create_best_provider()`, l.420).
- It downloads from HuggingFace only when the file is missing (l.215).
- It leaves LLM rerank on by default (l.417). The gate turns it off; running the bench directly does not.

**Missing:** a graded fixture, a breakdown per class, an abstention scorer and a citation checker.

**Cost:** medium.

## D. Hooks, MCP and session start (priority 2)

**Status:** the Qdrant point is confirmed by code but conditional. The latency figures are a hypothesis.

**Evidence**
- **Qdrant.** The static review's claim is **partly wrong**. `MemoryService.__init__` calls `_init_qdrant()` (service.py:404), which returns `None` unless `QDRANT_URL` is set (l.420-422). Only when it is set does it build the backend and call `ensure_collection()`, which does network I/O. Whether the hook environment sets it is unverified.
- **Embeddings provider.** It is lazy (l.406-411).
- **Hook.** The warm path is a stdlib `urllib` POST to `/hook/recall` with a 4 s timeout (`remote.py:26`, `:62`). If the call times out after reaching the server, the result is `skipped_busy` and there is no local recall. Only an unreachable server falls back to a cold local recall.
- **Classify test.** The static review is correct here. `tests/test_classify_hook_latency.py` measures only the in-process `classify()` (l.60-63) against a 15 ms median budget. It excludes interpreter start and imports.
- **Recall latency test.** `tests/test_recall_latency.py` patches `MemoryService`, so it checks telemetry fields, not time.
- **Live measurement (review of 2026-10-04):** `via=shared` 68-79 %, `skipped_busy` 21-32 %. The cause is the shared server being paged out while idle.

**Hypothesis:** process start, imports and opening the database dominate the cold cost, not `classify()`.

**Metrics:**
- Primary: p50/p95 of the real hook subprocess, cold, against the warm POST.
- Secondary: time per stage and the split by `via`.
- Constraint: read-only database, no spool writes.

**Side effects:** `scripts/bench_recall_latency.py` points at the live database (l.22) and calls `init_db()` twice (l.93-96). **Do not run it against the real database.**

**Missing:** a subprocess-level hook timer against a temp database.

**Cost:** low to medium.

## F. Jev: calibration, ranking and abstention (priority 2)

**Status:** confirmed by code. Values from the real ledger are not evaluated.

**Evidence**
- **Labels.** `used_in_turn` is a detector label (outcomes.py:252), not truth. `used_in_turn_weak` is kept out of calibration (outcomes.py:55, 259). The `jev`, `unattributed_override`, `unknown_actor` and `unknown` sources are excluded (metrics.py:46). Operator truth labels come from the review queue and are therefore biased; the script itself warns about this.
- **Calibration.** `scripts/jev_calibration_report.py` reports Brier, ECE and reliability, plus a split-half threshold. It requires at least 100 outcomes and a 95 % Wilson lower bound at or above the precision target. It opens the ledger read-only.
- **OPE.** `scripts/jev_ope.py` already reports IPS, SNIPS and DR with n, ESS (warning below 30), clipping (`min(pi/mu, 10)`), support and a bootstrap interval. **The static review's concern about this is outdated.**
- **Exploration.** It only permutes the top 5 (`RANKING_TOP_K = 5`, policy.py:135). Changing k has no support.
- **Abstention.** There is no abstention metric. A held decision counts as having no verdict.
- **Live (measured 2026-10-04, 72 h):** strong use was recall 1/1066 and session 3/277. Calibration cannot start.

**Hypothesis:** thresholds fitted on proxy labels miss real relevance.

**Metrics:**
- Primary: ECE and precision (Wilson lower bound) on an unbiased label sample.
- Secondary: ESS, clipped fraction, support, interval width.

**Missing:** random operator labels, not selected by the queue, and exploration of k.

**Cost:** high.

## N. Models, cost and optional paths (priority 3)

**Status:** confirmed by code. Real spend is not evaluated.

**Evidence**
- **Per-cycle budget.** `core/llm_budget.py` sets per-cycle caps on calls, tokens and failures per provider.
- **Token counts are estimated, not real.** `estimate_tokens` is `(chars + 3) // 4` (l.152-155). It is recorded after the call (`llm_provider.py:772-775`), so the cap can only stop the *next* call.
- **Durable daily quota.** It counts calls, not tokens. Reservations always close as `attempted` (llm_provider.py:769-771), so failures are not told apart.
- **Fallback.** There is a single optional fallback provider, triggered by an empty response or a quota error.
- **Budget policy.** `evaluation/budget_policy.py` is a shadow policy with `provider_calls: 0` hard-coded. It does not control spend.
- **Postgres.** It is selected for a Postgres DSN (store_factory.py:33). SQLite remains authoritative.

**Hypothesis:** the char/4 estimate distorts the token cap and the cost reports.

**Metrics:**
- Primary: estimated against provider-reported tokens per call.
- Secondary: how often fallback fires, aborts by reason.

**Missing:** capture of the usage the provider reports, and a durable quota by tokens.

**Cost:** medium.
