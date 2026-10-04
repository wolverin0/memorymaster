# MemoryMaster autoresearch, first run (2026-10-04)

This is ROADMAP item 7. It responds to the prompt "MemoryMaster: diagnóstico y autoresearch medible", which was written for Codex. This run covers the diagnosis and the instruments. **No optimization loop was started.** Campaigns run only after the operator approves.

## Contents

| # | Deliverable | File |
|---|---|---|
| 1 | Effective architecture map and inspection coverage | this file |
| 2 | Campaign catalog A-P, corrected against the code | `catalog-A-B-O-P.md`, `catalog-C-D-F-N.md`, `catalog-E-G-H-I-K.md`, `catalog-J-L-M.md` |
| 3 | Instrument inventory and evidence that the evaluators reject bad results | `INSTRUMENTS.md` |
| 4 | Measured baselines, plus what remains unmeasured | `BASELINES.md` and `baseline-*.json` |
| 5-6 | Three manifests, with launch commands and prompts | `MANIFESTS.md` |
| 7 | Decisions per campaign | `DECISIONS.md` |
| — | External controllers that were read | `EXTERNAL-CONTROLLERS.md` |
| 8 | Reproducible files | `benchmarks/pack_context_bench.py`, `benchmarks/tokenizer_cold_bench.py`, `benchmarks/recall_warm_bench.py`, `tests/test_autoresearch_bench_guards.py`, `tests/test_autoresearch_perf_gate.py` |

## 1. Effective architecture and identity

**Identity.**
- Base commit `31c9492` on branch `main`. This run added `bc8493e` (perf gate) and `0aec7bb` (compaction). The bench and doc commits follow.
- Python 3.12.4. `memorymaster` 4.9.0 is imported from the checkout. The two runtimes installed on the machine are also 4.9.0.
- The tree was dirty with unrelated operator changes (`.codex/config.toml`, `DOCS-MAP.md`, `docs/code-navigation.md`, the auto-ingest hook template). They were not touched.

**Code versus live state.** The table separates what was read in code, what was measured on disposable data, and what was observed live (read-only).

| Layer | Code (inspected) | Measured on disposable data | Observed live, read-only |
|---|---|---|---|
| Authoritative SQLite (`stores/`) | Generation triggers on 16 columns (`0004_query_cache.py:44-55`) | — | Generation 3,172,382; about 46k live claims; 4,755 confirmed |
| Prompt recall (`recall/context_hook.py`) | Scopes: current, `global`, `project`; confirmed only (`planner.py:96`); format `text` | Warm and cold recall at 45k (`BASELINES.md` §5) | Shared server `hook_recall` median 660 ms, p90 9.2 s; `skipped_busy` 21-32 % |
| Tokenizer (`recall/recall_tokenizer.py`) | `_corpus_stats` scans the whole corpus; cache per process and generation | Cold 752 ms at 45k, warm 6 ms | — |
| Packing (`recall/context_optimizer.py`) | Quadratic re-render | JSON 200 rows: 458 ms median | The hook uses `text` |
| Capture (`capture/`) | One job per lease, 300 s lease | ACK p95 69 ms, integrity clean | 95 capture jobs blocked, stable |
| Graph observations (`knowledge/`) | Supports require evidence lineage (`graph_observation_repository.py:347-380`) | — | 3 of 4,755 confirmed claims have evidence links; 0 observations since 2026-08-31 |
| Profile (`profile/`) | Greedy render, 1400 tokens | — | Run 6 completed; 46 of 63 facts rendered |
| Compaction (`govern/jobs/compact_summaries.py`) | Clustered across scopes and tenants (**fixed**) | Red-then-green test | Only reachable through the manual CLI |
| Steward classifier | Off by default; v2/v3 path mismatch | — | Inactive: no variable set |
| Jev (`decisions/`) | Calibration and OPE with n, ESS and intervals | — | Strong use: recall 1/1066, session 3/277 |

**Inspection limits.**
- The catalog was produced by four parallel read-only agents plus direct verification of the points that mattered:
  - the compaction leak (code read and reproduced with a test);
  - the perf gate (code read);
  - the generation triggers (migration read);
  - the graph funnel (measured live, read-only).
- These were **not** read in full: the body of `service.query_rows`, the internals of `graph_expansion`, and `scripts/llm_benchmark.py`.
- Nothing ran against the real database except read-only `SELECT`s.

## Safety record of this run

- Provider calls: 0. The perf gate measures outbound connections and blocks any that are not loopback.
- Paid judges: none.
- Writes to the real database: none.
- Live configuration: not changed. No flags, schedulers or services were changed for this work.
- Temporary corpora were built under the OS temp directory and removed afterwards.
- Every benchmark removes the live configuration and redirects HOME.
- External repositories were read through the web only. Nothing was installed, including the `codex-autoresearch` skill, which awaits approval.
