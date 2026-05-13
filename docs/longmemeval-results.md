# LongMemEval-S Results

## TL;DR

MemoryMaster v3.14.0 on LongMemEval-S cleaned, full retrieval run dated 2026-05-13: N=500, R@5 **0.8940**, R@10 **0.9420**, MRR **0.7992**. Full QA accuracy is **partial only**: 0/3 judged correctly before Gemini quota stopped the fallback judge.

## Methodology

- Dataset: `xiaowu0162/longmemeval-cleaned`, file `longmemeval_s_cleaned.json` (LongMemEval-S cleaned, 500 questions total).
- Run scope: all 500 questions for retrieval.
- Ingest path: one MemoryMaster claim per haystack session by default. The dataset `haystack_session_id` is stored in `source_agent`, `subject`, and citation source. The canonical harness also supports `--chunk-chars` for old-script-style chunked ingest experiments.
- Retrieval path: `MemoryService.query_rows(..., retrieval_mode="hybrid")` over a fresh per-question ephemeral SQLite store, with `vector_hook` disabled. This uses MemoryMaster's hybrid lexical ranker over claims, not Qdrant and not OpenAI embeddings.
- Store isolation: each question uses a fresh temporary SQLite database and does not read or write `memorymaster.db`.
- Top-K: 10 for retrieval metrics; top-5 retrieved claims are used as context for full QA mode.
- Metric definition: R@K is counted as a hit if any `answer_session_id` appears in the top K retrieved session ids. MRR uses the rank of the first relevant session.
- Judge path: `gpt-4o` with tenacity retries first; after OpenAI exhausted retries, the harness switched to `gemini-2.5-flash`. Gemini quota stopped QA after 3/500 questions, so QA is not a full benchmark number.

## Results

| Scope | Date | Questions | R@5 | R@10 | MRR | Full-QA accuracy |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| MemoryMaster v3.14.0 | 2026-05-13 | 500 | 0.8940 | 0.9420 | 0.7992 | 0.0000 (3/500 partial) |
| agentmemory published reference | n/a | 500 | 0.9520 | 0.9860 | 0.8820 | n/a |

Reference comparison is included for orientation only. The MemoryMaster row is N=500 retrieval; its QA cell is a tiny partial and should not be compared as a full accuracy score.

## Breakdown

### Retrieval By Question Type

| Question type | Questions | R@5 | R@10 | MRR |
| --- | ---: | ---: | ---: | ---: |
| knowledge-update | 78 | 0.9615 | 0.9615 | 0.9171 |
| multi-session | 133 | 0.9549 | 0.9850 | 0.8402 |
| single-session-assistant | 56 | 0.9107 | 0.9643 | 0.8614 |
| single-session-preference | 30 | 0.4000 | 0.6333 | 0.2616 |
| single-session-user | 70 | 0.8857 | 0.9429 | 0.7599 |
| temporal-reasoning | 133 | 0.9023 | 0.9474 | 0.8049 |

### QA By Question Type

| Question type | Judged | Correct | QA accuracy |
| --- | ---: | ---: | ---: |
| single-session-user | 3 | 0 | 0.0000 |

QA stopped at 3/500 because both OpenAI retries and the Gemini fallback quota were exhausted during the run.

## Reproduce

```powershell
python tests\bench_longmemeval.py --retrieval-only
python tests\bench_longmemeval.py --full
```

The dataset is downloaded on demand to `benchmark/data/longmemeval_s_cleaned.json`, which is gitignored. Results are written to `benchmark/longmemeval_s_results.json`.

## Cost And Runtime

- Retrieval: 500 questions in 1,069.787 seconds.
- End-to-end run: 1,175.810 seconds.
- Judge models used: `gpt-4o` attempted first, then `gemini-2.5-flash` for completed QA calls.
- Tokens consumed in completed judge calls: 14,948.
- QA status: partial, 3/500 judged, stopped by Gemini quota after fallback.
