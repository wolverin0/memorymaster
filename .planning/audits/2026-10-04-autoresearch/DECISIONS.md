# Decisions per campaign (2026-10-04)

| Campaign | Decision | Why |
|---|---|---|
| **B** Context packing | **Start now** (campaign 2) | Confirmed by code and measured: 458 ms median for 200 rows in JSON versus 5.5 ms in XML. It is pure code with an exact Guard (fingerprint plus golden tests) and zero risk to data. |
| **A** Cold tokenizer | **Start now** (campaign 1) | Measured: linear, 752 ms at 45k. Measured in recall: one write takes warm recall from 69 ms to 681 ms, matching the live 660 ms median. Exact Guard (tokens). The scope excludes triggers and migrations. |
| **D** Warm recall, hooks | **Measure first, then launch** (campaign 3) | The live median is 660 ms and the harness exists (`recall_warm_bench.py`). The dominant stage still needs profiling before the scope can be narrowed. |
| **J** Graph observations | **Product decision, not a campaign** | Measured: 3 of 4,755 confirmed claims have an evidence link, so supports cannot form. Each run enqueues about 150 jobs that are empty by construction. The options are to stop enqueuing discovery where a scope has no eligible supports (cheap) or to give citation claims an evidence chain (design change). ROADMAP item 8. |
| **K** Compaction | **Done as a fix, not a campaign** | It mixed scopes and tenants in one confirmed summary. Fixed in `0aec7bb` with a red-then-green test. |
| **I** Steward classifier | **Exclude for now** | Measured: inactive live (no `MEMORYMASTER_STEWARD*` variable). The legacy formula decides. The v2/v3 mismatch only matters if someone enables it. |
| **C** Retrieval quality | **Measure first** | There are no graded Spanish/English labels and no abstention or citation scorer. Without labels any ranking campaign would optimize a public slice indefinitely. |
| **F** Jev calibration | **Postpone (labels)** | About 1-3 positives per question against 100 needed. `used_in_turn` is not truth. OPE has no support for k. |
| **H** Dreaming | **Postpone (labels)** | Needs human-labelled cohorts. The selection script calls providers. |
| **E** Capture | **Postpone** | Measured: p95 ACK 69 ms, integrity clean. End-to-end is unmeasured, but nothing hurts today. |
| **G** S1 budget | **Measure first** | The 09-24 stop has a plausible cause (a ledger-wide 60 s window). It can be simulated with the Pacer's fake clock, but the simulation is not built. |
| **M** Recovery | **Postpone (decision)** | Peak RAM is plausibly several times 7.6 GB. Streaming needs a new encrypted format with migration. That is a design decision, not a loop. |
| **O** Vector scoring | **Postpone** | Real effect depends on vectors being enabled live, which is unverified. If D's profiling points there, it moves into D. |
| **P** SQL fan-out | **Exclude for now (risk)** | Collapsing queries can change coverage per token and ranking. If D shows the fan-out dominates, it gets its own campaign with the ids Guard. |
| **L** Dashboard | **Postpone** | No live pain. The test's premise was wrong: it does not measure page load. |
| **N** Cost/tokens | **Postpone** | The char/4 estimate and the per-call quota are hygiene. Jev's daily spend is $0.02-0.06 against a $2 cap. |

## What this run delivered beyond the catalog

- **Fixed:**
  - Hermetic perf gate that measures outbound calls (`bc8493e`).
  - Compaction isolation leak (`0aec7bb`).
- **New instruments with exact Guards:**
  - `benchmarks/pack_context_bench.py`
  - `benchmarks/tokenizer_cold_bench.py`
  - `benchmarks/recall_warm_bench.py`
  - Tests proving each Guard rejects a real degradation.
- **Out of scope for a loop:** the graph observation design gap (measured), plus three findings left for the operator:
  - the steward classifier is inactive;
  - `corpus_generation` bumps on non-text updates;
  - prompt recall's `skipped_busy` comes from the paged-out server, not from code.

## Operator decision suggested (outside the loop)

`corpus_generation` goes up on any update of 16 columns, including `confidence`, `updated_at` and `last_validated_at` (`0004_query_cache.py:44-55`). The tokenizer's statistics depend only on text and on which claims are live. A separate generation for text and live membership would avoid most rescans, and would likely help more than any optimization inside the tokenizer. It needs a migration and a decision about the query cache, which also uses that generation. It is not something an autonomous loop should do.
