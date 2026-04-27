# Recall weight grid — precision@5 tuning

- Eval prompts: `artifacts\real-prompts-1000.jsonl` (100, 70 labeled)
- DB: `memorymaster.db` (post-L2-backfill snapshot)
- Grid: W_LEXICAL × W_FRESHNESS × W_GRAPH = 3 × 3 × 4 = 36 cells
- Total wall: 553.8s

## Top 10 by precision@5

| W_LEXICAL | W_FRESHNESS | W_GRAPH | precision@5 | MAP@5 | hit@5 | p95 ms | wall s |
|---|---|---|---|---|---|---|---|
| 0.2 | 0.0 | 0.0 | 0.105 | 0.188 | 0.234 | 15.7 | 10.6 |
| 0.2 | 0.0 | 0.05 | 0.105 | 0.188 | 0.234 | 85.2 | 17.0 |
| 0.2 | 0.0 | 0.1 | 0.105 | 0.188 | 0.234 | 85.3 | 17.3 |
| 0.2 | 0.0 | 0.2 | 0.105 | 0.188 | 0.234 | 87.6 | 18.0 |
| 0.2 | 0.05 | 0.0 | 0.105 | 0.187 | 0.234 | 16.5 | 11.0 |
| 0.2 | 0.05 | 0.2 | 0.105 | 0.187 | 0.234 | 84.5 | 16.6 |
| 0.2 | 0.05 | 0.1 | 0.105 | 0.187 | 0.234 | 85.8 | 17.0 |
| 0.2 | 0.05 | 0.05 | 0.105 | 0.187 | 0.234 | 90.2 | 17.9 |
| 0.3 | 0.05 | 0.0 | 0.105 | 0.184 | 0.235 | 14.7 | 10.0 |
| 0.3 | 0.0 | 0.0 | 0.105 | 0.184 | 0.235 | 15.1 | 10.1 |

## Winner

`MEMORYMASTER_RECALL_W_LEXICAL=0.2` `MEMORYMASTER_RECALL_W_FRESHNESS=0.0` `MEMORYMASTER_RECALL_W_GRAPH=0.0`

precision@5 = **0.105** (baseline 0.152, delta = -0.047)
