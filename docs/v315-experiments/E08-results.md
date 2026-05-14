# EXPERIMENT v315-E08 - FTS5 porter stemming

## Hypothesis

The locked E01 baseline used R@5 = 0.9660, R@10 = 0.9840, and MRR = 0.9021. This experiment tested whether switching the SQLite FTS5 `claims_fts` tokenizer from the default `unicode61` behavior to `porter unicode61` would recover matches where queries and stored claims differ only by English morphology.

Predicted Delta R@5: +0.0050 to +0.0100.

## Change

`memorymaster/_storage_schema.py` now creates `claims_fts` with:

```sql
tokenize='porter unicode61'
```

`memorymaster/schema.sql` does not contain a `claims_fts` virtual table definition, so there was no duplicate SQLite schema statement to update. `memorymaster/schema_postgres.sql` was left unchanged because PostgreSQL uses `tsvector`, not SQLite FTS5.

The benchmark initializes fresh ephemeral SQLite databases, so no migration was required for this experiment.

## Metrics

Benchmark command:

```powershell
python tests/bench_longmemeval.py --retrieval-only
```

| Run | R@5 | R@10 | MRR | Delta R@5 | Verdict |
|---|---:|---:|---:|---:|---|
| E01 baseline | 0.9660 | 0.9840 | 0.9021 | 0.0000 | baseline |
| E08 porter tokenizer | 0.9660 | 0.9840 | 0.9021 | 0.0000 | NULL |

## Decision

**NULL**. R@5 was unchanged from the locked baseline, so `|Delta R@5| < 0.005`. Pytest passed, but the experiment does not show a measurable retrieval quality gain.

## What Next

Ship v3.15.0.

## Verification

- `python tests/bench_longmemeval.py --retrieval-only`: R@5 = 0.9660, R@10 = 0.9840, MRR = 0.9021.
- `python -m pytest tests/ -q --tb=line -x`: 2058 passed, 46 skipped, 1 xfailed, 1 warning in 565.84s.
