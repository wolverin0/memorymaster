# Existing instruments, gaps, and proof that the evaluators reject bad results

Checkout `31c9492` plus this run's commits, Python 3.12.4. `memorymaster` is imported from the checkout (4.9.0).

## Inventory

| Instrument | What it really measures | Gap, or fix made in this run |
|---|---|---|
| `scripts/autoresearch_perf_gate.py` + `benchmarks/perf_smoke.py` | Ingest, query and steward cycle on synthetic claims in a temp DB. Defaults: 80 claims, 30 queries, 3 repeats. | **Fixed `bc8493e`.** `provider_calls` was a literal 0 (`:74`), and the gate only removed Qdrant and rerank from the environment. With the operator's live shell (Jev live, real home) it could reach providers and write the real ledger. It now runs hermetically, refuses and counts non-loopback connections, and fails above `--max-provider-calls`. Still open: it reports the *median of each run's p95*, not a global p95, and the scale is small. |
| `scripts/bench_recall_latency.py` | `recall()` end to end. | **Do not run without a disposable DB.** It defaults to the live DB and calls `init_db()` twice. |
| `tests/test_recall_latency.py` | Patches `MemoryService`, so it checks telemetry fields, not time. | Not a latency instrument. |
| `scripts/autoresearch_longmemeval_gate.py` | Recall@5/10 and MRR at session level, on a window of LongMemEval-S. | Already enforces a policy (`3e0fcc4`). There is no nDCG, no abstention metric, and the 30 `_abs` questions are scored like the rest. |
| `scripts/autoresearch_longmemeval_qa.py` | QA with a judge, resumable in chunks. | Already binds chunks to dataset, retrieval, benchmark, judge and revision hashes (`3e0fcc4`). The judge makes paid calls, so it is not for this phase. |
| `benchmarks/capture_ack_benchmark.py` | ACK p95, duplicates, orphans and secret leakage, with a fake extractor. | It has no end-to-end, throughput, queue-age or crash-recovery measurement. |
| `tests/test_classify_hook_latency.py` | `classify()` inside the process, against a 15 ms median budget. | It excludes interpreter start, imports and stdio. |
| `tests/test_dashboard_latency.py` | Latency from claim creation to validation (`/metrics/validation-latency`). | It does **not** measure page load. The static review's premise is partly wrong. |
| `scripts/jev_calibration_report.py`, `scripts/jev_ope.py` | Brier/ECE/Wilson calibration. OPE with IPS, SNIPS and DR, plus n, ESS, clipping and a bootstrap interval. | The OPE concern is outdated: n, ESS, clipping and intervals are already reported. Exploration only permutes the top 5, so k has no support. The labels are a usage proxy. |
| `memorymaster/dreaming/evaluation.py`, `cohort_evaluation.py` | Thresholds 0.90/0.85/0.95…, and omission rows labelled by hand. | No duplication or cost-per-useful metric. `scripts/evaluate_dreaming_selection.py` **calls providers**. |
| **New** `benchmarks/pack_context_bench.py` | `pack_context` time per size and format, plus an exact fingerprint of the output. | Campaign B's Guard. |
| **New** `benchmarks/tokenizer_cold_bench.py` | Cold and warm `extract_query_tokens` per corpus size, in fresh processes with the live configuration removed, plus a fingerprint of the tokens. | Campaign A's Guard. |

## Evidence that the evaluator rejects bad results

Each case the prompt asks for has an executable test that fails on purpose and has to be rejected:

| Case | Test |
|---|---|
| Degraded ranking when equivalence is required | `tests/test_autoresearch_longmemeval_gate.py::test_gate_rejects_degraded_ranking_incomplete_slice_and_provider_calls` |
| Incomplete result | Same test (incomplete window) |
| Dataset changed while keeping the IDs | `tests/test_autoresearch_longmemeval_qa.py::test_resume_rejects_a_chunk_whose_inputs_changed` (dataset parameter) |
| Stale chunk from another candidate | Same test (revision parameter), plus `test_resume_rejects_a_legacy_chunk_without_a_fingerprint` |
| Unexpected external calls | `tests/test_autoresearch_perf_gate.py::test_an_outbound_connection_is_blocked_counted_and_fails_the_gate`, and the LongMemEval gate (`provider_calls`) |
| Live configuration or real home leaking into the benchmark | `tests/test_autoresearch_perf_gate.py::test_the_live_configuration_and_real_home_never_reach_the_benchmark` |
| A faster but degraded output (a claim dropped) | `tests/test_autoresearch_bench_guards.py::test_pack_context_guard_rejects_a_dropped_claim_and_accepts_identical_output` |
| Different tokens selected | `tests/test_autoresearch_bench_guards.py::test_tokenizer_guard_rejects_changed_tokens` |
| Privacy: secret leakage | `tests/test_capture_ack_benchmark.py` (`leaked_secret_rows == 0`) |
| Isolation: mixing scopes or tenants | `tests/test_compact_summaries.py::test_clusters_never_cross_scope_or_tenant`. This test found a real bug, fixed in `0aec7bb`. |

No test, threshold or fixture was weakened to make anything pass.
