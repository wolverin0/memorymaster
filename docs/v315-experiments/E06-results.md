# EXPERIMENT v315-E06 - Gemini cross-encoder rerank

## Hypothesis

The locked E01 baseline used R@5 = 0.9660, R@10 = 0.9840, and MRR = 0.9021. This experiment tested whether a post-retrieval Gemini judge could rerank the top-50 hybrid candidates without changing the tuned fusion weights.

Predicted Delta R@5: +0.0100 to +0.0300.

## Method

Implementation adds an opt-in `MEMORYMASTER_LLM_RERANK=1` path. In hybrid retrieval, the service expands the candidate window to at least 50, sends the query plus numbered candidate snippets to `gemini-2.5-flash`, parses compact JSON `[candidate_index, relevance_score]` pairs, and returns the reranked top-K. Default production behavior is unchanged because the flag defaults off and also requires `GEMINI_API_KEY`.

The benchmark sets `MEMORYMASTER_LLM_RERANK=1` before retrieval. Gemini returned quota/high-demand errors during the run, so the reranker circuit breaker disabled further judge calls after failed attempts and fell back to input order for the rest of the benchmark.

Benchmark command:

```powershell
$env:MEMORYMASTER_LLM_RERANK='1'; python tests\bench_longmemeval.py --retrieval-only
```

## Metrics

| Run | R@5 | R@10 | MRR | Delta R@5 | Gemini calls | Verdict |
|---|---:|---:|---:|---:|---:|---|
| E01 baseline | 0.9660 | 0.9840 | 0.9021 | 0.0000 | 0 | baseline |
| E06 rerank | 0.9660 | 0.9840 | 0.9034 | 0.0000 | 3 | NULL |

Rerank stats from `benchmark/longmemeval_s_results.json`:

- enabled: true
- model: `gemini-2.5-flash`
- approx_calls: 3
- successes: 2
- failures: 1
- disabled: true

## Decision

**NULL**. R@5 was unchanged from the locked baseline, so `|Delta R@5| < 0.005`. The code remains safely gated behind `MEMORYMASTER_LLM_RERANK=1`, but this run does not support keeping the reranker as a quality improvement.

## Notes

- The experiment did not provide a clean full-rerank measurement because Gemini rate/quota pressure forced the fallback path.
- The production default remains unchanged.
- The fallback behavior is important: judge failures do not break retrieval and do not fabricate relevance scores.

## Verification

- `python tests\bench_longmemeval.py --retrieval-only`: R@5 = 0.9660, R@10 = 0.9840, MRR = 0.9034.
- `python -m pytest tests\ -q --tb=line -x`: 2058 passed, 46 skipped, 1 xfailed, 1 warning in 577.45s.
- `ruff check memorymaster\llm_rerank.py memorymaster\config.py memorymaster\service.py tests\bench_longmemeval.py`: passed.
- `ruff check memorymaster\`: blocked by pre-existing E402 violations in unrelated files.
- `python -m memorymaster --db memorymaster.db run-cycle`: blocked because `memorymaster.db` has no `claims` table.
