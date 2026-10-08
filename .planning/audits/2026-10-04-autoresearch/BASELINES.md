# Measured offline baselines (2026-10-04)

**Setup.**
- Hardware: Intel i9-14900K (32 threads), 64 GB RAM, Windows 10, Python 3.12.4.
- Software: checkout `31c9492` plus this run's commits; the `memorymaster` package is imported from the checkout.
- Isolation: every run uses disposable databases with the live configuration removed (`MEMORYMASTER_*`, provider keys, `QDRANT_URL`) and HOME pointed at a temp dir. Outbound calls were 0 (measured by the perf gate, not assumed).

**Noise caveat.** The machine was not idle. The shared MCP server, Orca and other panes were running, with 2-6 GB free of 64. Treat spread as real. Compare a candidate only against a baseline run in the same session, in alternating order.

## 1. Performance smoke (`scripts/autoresearch_perf_gate.py`, hardened in `bc8493e`)

Configuration: 80 claims, 30 queries, 2 steward cycles, 7 independent repeats.

| Metric | Value |
|---|---|
| query p95 (median of 7 runs) | 48 ms |
| query p95 spread across runs | 56 ms |
| ingest p95 | 29 ms |
| steward cycle p95 | 3.1 s |
| total per run | 8.0 s |
| provider calls (measured) | **0** |
| confirmed / expected | 80 / 80 |

The spread is larger than the value itself. At this scale the query p95 is too noisy to act as a campaign metric. Keep it as a smoke test.

## 2. Context packing (`benchmarks/pack_context_bench.py`, campaign B)

Raw data: `baseline-pack-context.json`. Pure Python: 9 repeats, warmup, budget 16,000 tokens.

| Rows | text median / p95 | xml median / p95 | json median / p95 |
|---|---|---|---|
| 20 | 0.27 / 0.35 ms | 0.25 / 0.36 ms | 8.3 / 10.5 ms |
| 50 | 0.60 / 0.99 ms | 1.06 / 1.10 ms | 49.7 / 59.9 ms |
| 200 | 5.2 / 7.7 ms | 5.5 / 8.0 ms | **457.8 / 501.4 ms** |

The JSON cost is superlinear: 2.5× the rows costs about 6× the time from 20 to 50, and 4× the rows about 9× from 50 to 200. Text and XML stay in single-digit milliseconds on the same rows. This is consistent with the per-candidate re-serialization found in the code (`context_optimizer.py:360-411`).

Live relevance: the prompt hook uses `text` (`context_hook.py:1346`). JSON reaches `query_for_context` through MCP and the Jev surfaces that take a format.

## 3. Tokenizer cold and warm cost (`benchmarks/tokenizer_cold_bench.py`, campaign A)

Raw data: `baseline-tokenizer-cold.json`. Each repeat runs in a fresh child process, 7 repeats.

| Live claims | cold median | cold max | warm median | corpus build |
|---|---|---|---|---|
| 1,000 | 26.8 ms | 30.6 ms | 4.9 ms | 11 s |
| 5,000 | 85.8 ms | 99.1 ms | 5.8 ms | 66 s |
| 20,000 | 323.4 ms | 366.1 ms | 6.2 ms | 377 s |
| 45,000 (live is about 46k) | **751.5 ms** | 806.9 ms | 5.8 ms | 1398 s |

- Cold cost is linear in corpus size, about 16-17 ms per 1,000 live claims. Warm cost is flat at about 6 ms.
- The fingerprint of the selected tokens is identical at every size.
- During the 5,000 case, a 40-claim guard test ran for a few seconds alongside the benchmark. The 5,000 cold max may include that interference; the median is unaffected in practice.

Live context, read-only:
- The live `corpus_generation` is 3,172,382. Triggers bump it on any update of `confidence`, `updated_at`, `last_validated_at`, `status` and others (`stores/migrations/0004_query_cache.py:44-55`).
- That invalidates the shared server's corpus statistics after every steward or ingest burst, even though the statistics depend only on text and membership in the live set.
- No bumps occurred in a quiet 120 s window. In the last 24 h there were about 8,000 confidence events.

## 4. Capture ACK (`benchmarks/capture_ack_benchmark.py`, campaign E)

Configuration: 40 text and 10 URL captures, fake extractor.

| Measure | Result |
|---|---|
| ACK p95, all captures | 68.7 ms |
| ACK p95, reference captures | 45.6 ms |
| ACK p95, replays | 49.9 ms |
| Duplicates, orphans, nonterminal jobs, leaked secrets | all 0 |
| Provider calls | 0 |

ACK is not a problem. End-to-end extraction time is unmeasured, because the benchmark fakes the extractor.

## 5. Warm recall (`benchmarks/recall_warm_bench.py`, campaign D)

Raw data: `baseline-recall-warm.json`. Configuration: 45,000 live claims, 10 % confirmed (live is about 4.8k confirmed of 46k), 5 queries × 7 repeats. Building the corpus took 1,258 s.

| State | median | p95 | max | n |
|---|---|---|---|---|
| cold (first call of the process) | 695.6 ms | — | — | 1 |
| **warm** (corpus unchanged) | **69.3 ms** | 83.1 ms | 88.1 ms | 35 |
| **first call after one write** | **681.0 ms** | 743.8 ms | 743.8 ms | 7 |

The five queries return five distinct rankings, so the ranking fingerprint is informative.

**Reading.**
- A single write (which bumps `corpus_generation`) makes the next recall about 10 times slower than warm.
- That extra cost matches the cold tokenizer scan at 45k (752 ms, §3), and the live median of the shared server (660 ms).
- This is **consistent with** the tokenizer rescan dominating live recall after every write burst. A profile has not yet **proved** it; profiling is the pre-launch step of campaign D.

Live reference, from the shared server's own log over the last 40 `hook_recall` calls: median 660 ms, p90 9.2 s. The p90 is the paging stall, the median is not.

## 6. Graph observation funnel (campaign J, read-only on the live DB)

| Item | Count |
|---|---|
| Confirmed claims | 4,755 |
| Confirmed claims with a `claim_evidence_links` row | **3** |
| `entity_edge_supports` | 348 (legacy-v0 173, personal-v1 175), newest 2026-08-14 |
| `source_items` | 102 |
| `evidence_items` | 212 |
| `extract_graph` jobs blocked | 94 (73 `graph_claim_ineligible`, 20 `graph_claim_unavailable`, 1 ontology) |

Supports require capture-lineage evidence. Claims that arrive through MCP and hooks carry citations, not evidence links, so they can never feed the graph. Hypothesis (a) in the J catalog is confirmed.

## Still unmeasured, and why

- **End-to-end recall quality (C)** needs graded labels in Spanish and English, which do not exist yet.
- **LongMemEval QA** needs a paid judge, which is not allowed in this phase.
- **Jev calibration (F)** has about 1-3 positives per question where 100 are needed.
- **Dreaming quality (H)** needs human-labelled cohorts. `scripts/evaluate_dreaming_selection.py` calls providers.
- **Recovery peak RSS (M)** needs synthetic DBs of 50-800 MB and a format decision. Not run.
- **Dashboard (L)** has no harness. Low priority.
- **S1 budget simulation (G)** needs a multi-surface simulation on a fake clock, which has not been built.
- **Steward classifier (I), now measured.** No `MEMORYMASTER_STEWARD*` variable is set in the session, user or machine environment, so the learned gate is **inactive**. Promotions use the legacy formula. The v2/v3 artifact mismatch therefore has no live effect today. It would only bite if someone enabled the classifier with the default path.
