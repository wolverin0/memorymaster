# LongMemEval-S Results

## TL;DR

MemoryMaster v3.14.0 on LongMemEval-S v1 partial run (first 50/500 questions): R@5 **0.8800**, R@10 **0.9400**, MRR **0.7729**; full-QA accuracy **deferred** because GPT-4o returned repeated HTTP 429s before any completed judgment.

## Methodology

- Dataset: `xiaowu0162/longmemeval-cleaned`, file `longmemeval_s_cleaned.json` (LongMemEval-S cleaned, 500 questions total).
- Run scope: first 50 questions only. A 10-question smoke test estimated the full 500-question retrieval pass would exceed the 20-minute task guardrail, so this is a clearly labeled v1 partial number.
- Ingest path: one MemoryMaster claim per haystack session. The raw session transcript is stored in `Claim.text`; the dataset `haystack_session_id` is stored in `source_agent`, `subject`, and the citation source.
- Retrieval path: `MemoryService.query_rows(..., retrieval_mode="hybrid")` over the fresh per-question ephemeral SQLite store, with `vector_hook` disabled. This uses MemoryMaster's hybrid lexical ranker over claims, not Qdrant and not OpenAI embeddings.
- Store isolation: each question uses a fresh temporary SQLite database and does not read or write `memorymaster.db`. The current `SQLiteStore` opens new connections per operation, so the harness uses temporary SQLite files rather than `:memory:` to keep schema visible across connections.
- Top-K: 10 for retrieval metrics; top-5 retrieved claims are used as context for full QA mode.
- Metric definition: R@K is counted as a hit if any `answer_session_id` appears in the top K retrieved session ids. MRR uses the rank of the first relevant session.
- Judge model: `gpt-4o` for both answer generation and YES/NO judging in `--full` mode. Phase 3 did not complete because the API returned repeated HTTP 429s before question 1 completed.

## Results

| Scope | Questions | R@5 | R@10 | MRR | Full-QA accuracy |
| --- | ---: | ---: | ---: | ---: | ---: |
| MemoryMaster v3.14.0, v1 partial | 50 | 0.8800 | 0.9400 | 0.7729 | deferred |
| agentmemory published reference | 500 | 0.9520 | 0.9860 | 0.8820 | n/a |

Reference comparison is included for orientation only. The MemoryMaster row is a first-50 partial run dated 2026-05-13; the agentmemory row is the published reference cited in claim `mm-d469`.

## Breakdown

| Question type | Questions | R@5 | R@10 | MRR | Full-QA accuracy |
| --- | ---: | ---: | ---: | ---: | ---: |
| single-session-user | 50 | 0.8800 | 0.9400 | 0.7729 | deferred |

The first 50 dataset rows contain only `single-session-user` examples, so this v1 run does not yet cover multi-session, temporal-reasoning, knowledge-update, or preference categories.

## Reproduce

```powershell
python tests\bench_longmemeval.py --retrieval-only --limit 50
python tests\bench_longmemeval.py --full --limit 50
```

The dataset is downloaded on demand to `benchmark/data/longmemeval_s_cleaned.json`, which is gitignored. Retrieval output is written to `benchmark/longmemeval_s_retrieval.json`.

## Cost And Runtime

- Retrieval-only v1 run: 50 questions in 170.535 seconds.
- Full QA: deferred. The run retried GPT-4o calls after HTTP 429 responses and stopped before any successful answer/judge pair, so no completed QA accuracy or meaningful token cost is reported.
