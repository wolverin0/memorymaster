# Benchmarks

## LongMemEval

[LongMemEval](https://github.com/xiaowu0162/LongMemEval) is a 500-question benchmark
for long-context conversational memory retrieval. We use it to score MemoryMaster's
two retrieval backends:

- `longmemeval_runner.py` — FTS5 keyword search baseline
- `longmemeval_vector_runner.py` — Qdrant vector search (OpenAI text-embedding-3-small, 1536 dims, Cosine)

### Setup

The oracle file (`longmemeval_oracle.json`, ~15 MB) is **not committed** to the repo
because it is a public dataset. Download it before running the benchmarks:

```bash
# Option A — direct from the LongMemEval repo
curl -L -o benchmarks/longmemeval_oracle.json \
  https://raw.githubusercontent.com/xiaowu0162/LongMemEval/main/data/longmemeval_oracle.json

# Option B — git clone the upstream repo and copy
git clone --depth 1 https://github.com/xiaowu0162/LongMemEval /tmp/lme
cp /tmp/lme/data/longmemeval_oracle.json benchmarks/longmemeval_oracle.json
```

The oracle file format is documented in the upstream repo. Each question contains
a sessions array (timestamped chat history) plus a ground-truth answer.

### Running

```bash
# FTS5 baseline (full 500 questions, ~5 min)
python benchmarks/longmemeval_runner.py --db memorymaster.db --questions 500

# Vector search (requires QDRANT_URL env var, ~10 min for 500)
export QDRANT_URL=http://localhost:6333
export OPENAI_API_KEY=sk-...
python benchmarks/longmemeval_vector_runner.py --db memorymaster.db --questions 500
```

### Results history

| Backend | Score | Notes |
|---------|-------|-------|
| FTS5 keyword | 5.6% (28/500) | Lexical only — fails on paraphrased questions |
| Qdrant vector | 25% (5/20 sample) | Semantic match. Full-500 run pending. |
| MemPalace ChromaDB (reference) | 96.6% | Verbatim storage + reranking |

The gap between MemoryMaster and MemPalace is the verbatim-storage layer:
LongMemEval tests *retrieval over raw conversation*, while MemoryMaster's
sweet spot is *curated claims*. See `memorymaster/verbatim_store.py` for the
in-progress verbatim layer.

## Other benchmarks

- `perf_smoke.py` — internal latency smoke test (P50/P95 query times)
- `slo_targets.json` — SLO thresholds for CI gating
- `thresholds.json` — alarm thresholds used by `scripts/alert_operator_metrics.py`
