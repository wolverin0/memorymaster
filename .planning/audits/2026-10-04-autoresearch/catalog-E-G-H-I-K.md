# Campaigns E, G, H, I, K: capture, budget, dreaming, steward, compaction

Read-only static inspection at HEAD `31c9492` (2026-10-04). Nothing in this file was measured.

## E. Capture, spool and queues (priority 1)

**Status:**
- **Confirmed by code:** the structure described below.
- **Covered by the existing test:** integrity under replay.
- **Hypothesis:** end-to-end latency, throughput and recovery.

**Evidence:**
- `benchmarks/capture_ack_benchmark.py:226` (`run_benchmark`) uses a temp SQLite database. The extractor is patched (`:138`). It reports p95 ACK (`:207-210`), duplicates, orphans, non-terminal or leased jobs, and secret leakage.
- It does **not** measure end-to-end latency, throughput, queue age or recovery after interruption. With the extractor faked, "usable memory" is not measured.
- The capture worker leases one job at a time (`capture/worker.py:201`) for 300 s. Expired leases are retried up to 5 times (`capture/repository.py:143-156`).
- Spool drain:
  - `core/spool.py:283-306` renames files to `.draining` and replays leftovers after a crash.
  - `read_lines` reads the whole file (`:309`). The drain has no line, time or size cap.
  - A file is unlinked only after all its lines are processed, so a crash mid-file replays the whole file.

**Hypothesis:** the ACK does not depend on the extractor, but time to usable memory grows with queue depth because of the one-job-per-lease worker.

**Metrics:**
- **Primary:** p95 ACK, and p95 time from ACK to extracted claim.
- **Secondary:** jobs per second, maximum queue age, replays after a kill, drain lag.
- **Constraints:** zero duplicates, zero orphans, zero leaked secrets, zero provider calls.

**Reuse and gaps:**
- Extend `run_benchmark`. It uses a temp database and a fake extractor, and patches `os.environ` for the capture roots.
- **Missing:**
  - a fake extractor with latency;
  - a kill between `claim_files` and `unlink`;
  - a large single spool file.

**Cost:** low.

## G. S1 and shared budget (priority 2)

**Status:**
- **Confirmed by code:** the mechanism described below.
- **Hypothesis:** the cause of the 2026-09-24 stop.

**Evidence:**
- **Pacer:** `Pacer` (`govern/jev_batch.py:44-63`) is a token bucket. It runs at 0.8 × `rpm_cap` with a burst of 0.1 × cap. Its clock and sleep can be injected, so a fake clock already exists. `run_paced` uses concurrency 8 (`:69`).
- **S1 revalidation (`govern/jobs/revalidation.py`):**
  - Keyset pages are capped at 500 (`:72`, `:256`), and a cycle takes 500 claims by default (`:66`).
  - A claim already answered for its `(version, text_sha256)` is skipped for 30 days (`:169-196`).
- **Budget:** `Budget.check` (`decisions/policy.py:263-283`) stops in three cases: the daily USD cap is exceeded, the ledger's requests in the last 60 s reach `rpm_cap` (600), or the token bucket is empty.
- **Shared Pacer:** `govern/candidate_dedupe.py:594-637` uses the same Pacer.

**Hypothesis:** the Pacer targets 80 % of the cap, but the 60 s ledger count includes every surface. So concurrency 8 plus other surfaces can hit the cap. A pacing budget shared across surfaces would remove the `budget_exhausted` stops.

**Metrics:**
- **Primary:** share of a simulated backlog completed before a stop.
- **Secondary:** peak requests per 60 s window, ledger count versus Pacer, skip ratio.
- **Constraints:** no provider calls, no daily cap breached, no re-asking unchanged state, no sensitive claim sent.

**Reuse:** `tests/test_jev_revalidation.py`, `test_jev_batch.py`, `test_decisions_policy.py`, `tests/_jev_fakes.py`.

**Missing:** a multi-surface simulation on a shared fake clock.

**Cost:** low to medium.

## H. Dreaming extraction and consolidation (priority 2)

**Status:** confirmed by code. Nothing is measured, because no real cohort result was inspected.

**Evidence:**
- **Activation thresholds** (`dreaming/evaluation.py:11-22`):

  | Metric | Threshold |
  |---|---|
  | Useful precision | 0.90 |
  | Useful recall | 0.85 |
  | Evidence precision | 0.95 |
  | Ephemeral rejection | 0.90 |
  | Scope isolation | 1.0 |
  | Action accuracy | 0.85 |
  | Structured yield | 0.95 |
  | Human acceptance | 0.80 |
  | Labelled decisions | 50 or more |
  | Human reviews | 20 or more |

- **What `evaluate_records` computes:** it counts `missed_useful` and `unwanted_emissions` (`:77-82`). It has no duplication or cost metric.
- **Captures with no output:** `cohort_evaluation.py` accepts omission rows (`capture-<id>:omission:`, `:55`), but only when labelled by hand.
- **Cost per useful memory:** `review_cohort.py:57-64` returns `"cost_per_useful_memory": None`, so it is explicitly unmeasured.
- **The selection script is not offline:** `scripts/evaluate_dreaming_selection.py` calls Antigravity/Gemini.

**Hypothesis:** the biggest blind spot is useful facts that get omitted when a capture produces no candidates.

**Metrics:**
- **Primary:** useful recall, including omission rows.
- **Secondary:** duplicate emissions, tokens per accepted memory.
- **Constraints:** no ledger mutation, scope isolation 1.0, cohorts that do not overlap.

**Reuse:** the offline tests `tests/test_dreaming_cohort.py` and `test_dreaming_evaluation.py`.

**Cost:** medium. It needs labels.

## I. Steward, dedup and lifecycle (priority 1 for the activation check)

**Status:**
- **Confirmed by code:** the fallback on a version mismatch.
- **Not evaluable with current access:** whether the classifier is active live, because the live environment was not read.

**Evidence:**
- **Off by default:** the runtime classifier (`govern/steward_classifier.py`) activates only with `MEMORYMASTER_STEWARD_CLASSIFIER_PATH` or `..._ENABLED`.
- **Version mismatch:** the default path is `artifacts/steward-classifier-v2.joblib`, while `FEATURE_VERSION = "v3"` (`steward_features.py:35`). `load_classifier` returns `None` on a mismatch, and the validator then falls back silently to the legacy formula (`validator.py:257-265`, threshold 0.65).
- **Script side effects:** all four scripts open the database read-only. `train_steward_classifier.py` writes a joblib artifact. `eval_steward_pareto.py` writes a fixture and a report.
- **Leakage:** the backtest report warns that rows before 2026-04-09 were in the training corpus.

**Hypothesis:** if the deployed path is the default one, the learned gate is silently inactive.

**Metrics:**
- **Primary:** precision at the promotion threshold, out of sample.
- **Secondary:** disagreement with the legacy formula, fallback rate.

**Missing:** a runtime counter of classifier use versus fallback.

**Cost:** low for the activation check.

## K. Compaction (priority 1)

**Status:**
- **Confirmed by code:** the isolation gap.
- **Hypothesis:** the authority of summaries.

**Evidence:**
- **No scope grouping:** `govern/jobs/compact_summaries.py` clusters archived claims by lower-cased subject (`:83-89`) or by embedding. Neither path groups by **scope or tenant**.
- **Scope inheritance:** the summary copies the first claim's scope (`:378`) and sets no tenant. So a cluster with mixed scopes would quietly inherit one scope.
- **Auto-confirmed:** the summary is created as `confirmed` (`:383-390`), with `derived_from` links and a `compactor` event.
- **No trace validator was found.** The static review's premise is unverified or outdated.
- **Only one caller:** `service.compact_summaries` (`core/service.py:2033`), through the `compact-summaries` CLI. No scheduler or steward call was found.
- **Tests stay offline:** `tests/test_compact_summaries.py` patches `_call_llm`.

**Hypothesis:** subject-only clustering can merge claims across scopes and tenants. The auto-confirmed summary is then authoritative without steward review.

**Metrics:**
- **Primary:** clusters per run that cross a scope boundary. This should be 0.
- **Secondary:** share of summaries that cite every source, and `derived_from` failures.
- **Constraints:** scope and tenant isolation, promotion controlled by the steward, no provider calls.

**Reuse:** extend `tests/test_compact_summaries.py` with mixed scopes and a fake LLM.

**Cost:** low.
