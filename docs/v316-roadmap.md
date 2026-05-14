# v3.16+ improvement roadmap

After v3.15.0 landed at R@5 = 0.966 (leading agentmemory's 0.952) on LongMemEval-S, six follow-up experiments (E02 RRF, E04 session-diversity, E05 W_LEX sweep, E06 LLM cross-encoder, E08 porter stemming) all returned NULL or REVERT verdicts. The headline lesson: **the linear fusion blend is at a local optimum once vector signal is wired** — any change to the fusion weights, fusion algorithm, or top-K reordering degrades or fails to improve R@5.

The remaining gain candidates are **not** in the fusion layer. They are:

## Tier-S (likely to move R@5 above 0.97)

### S1. Unify weight constants between lexical-only and semantic-aware ranking paths

`memorymaster/retrieval.py` currently has two ranking paths: `rank_claim_rows` (lexical-only, reads `MEMORYMASTER_RETRIEVAL_WEIGHTS`) and the semantic-aware hybrid path (hardcoded blending). E05's W_LEX sweep REVERTed because the env override didn't reach the semantic branch, creating inconsistent ranking. Fix: thread `W_LEX / W_VEC / W_CONF / W_FRESH` through both paths as a single config dict.

Once unified, re-run the E05 sweep with proper weight propagation — the original hypothesis (bump W_LEX for dense Q&A) was sound, only the implementation was incomplete.

**Predicted Δ R@5**: +0.005 to +0.015. Architectural debt fix is the prerequisite.

### S2. RRF as a *boost* signal, not a *base* fusion

E02 REVERTed because RRF *replaced* the linear blend, throwing away magnitude information. Better: keep the linear blend as the base, then add RRF on top as a tiebreaker boost when the linear blend produces scores within ~0.01 of each other in the top-10. Inspired by v3.10's "F6 closets BOOST_ONLY" pattern from the `memorymaster-release-discipline` skill (claim mm-3f8d).

**Predicted Δ R@5**: +0.005 to +0.01. Lower risk than E02 because the base ranking is preserved.

### S3. Per-question-type retrieval profiles

`memorymaster/query_classifier.py` already classifies queries (factual / procedural / conversational / etc) but doesn't feed that classification into retrieval. LongMemEval-S has answer types with very different optimal weights (`single-session-user` → high W_LEX; `multi-session` → high W_VEC; `temporal-reasoning` → high W_FRESH). Wire the classifier output to swap weight profiles per query.

**Predicted Δ R@5**: +0.01 to +0.025. Highest-leverage non-trivial change. Requires S1 (unified weights) first.

## Tier-A (worth running, smaller leverage)

### A1. LongMemEval QA accuracy with judge

Retrieval-only is the easy benchmark. The full LongMemEval QA pipeline (retrieve → answer → judge) is what most other systems sandbag on. We've already built the harness (`--full` flag) and the Sonnet judge adapter; it's blocked only on `ANTHROPIC_API_KEY` not being in the shell env (per claim mm-2c65). Once the key is configured, run the full pipeline against the 0.966-retrieval candidates and publish the QA number alongside R@5.

Not a retrieval improvement — but a *credibility* win that no competitor publishes.

### A2. LongMemEval-M (500-session corpus)

LongMemEval-S has ~48 sessions per question (~115k tokens). The M variant has ~500 sessions per question — closer to real "many-month conversation history" scenarios. Per the upstream README, M doesn't fit in 128k context, so retrieval matters MORE. agentmemory doesn't publish M numbers either. First-mover advantage if our retrieval scales.

**Risk**: ingest cost is 10× per question; full 500q × M corpus is ~5h codex wall-clock. Worth a single run to publish the number.

### A3. Hybrid retrieval: trigram-similarity for typo robustness

SQLite FTS5 with porter stemming (E08, NULL) didn't help because LongMemEval-S has clean text. Real-world queries have typos. Add `pg_trgm`-style trigram similarity as a third lexical signal alongside BM25 + porter — useful for production even if no LongMemEval bump.

**Predicted Δ R@5 on LongMemEval-S**: ~0. Real-world: meaningful for typo-heavy queries.

## Tier-B (deferred / cost-only)

### B1. Zero-LLM entity graph extraction (gbrain pattern)

E07 was planned but never run. Current entity extraction is LLM-based — slow + costly per ingest. gbrain ships regex + heuristic typed-relation extraction at zero LLM cost. Doesn't help R@5 directly; helps make E03 (entity-graph RRF stream) viable by populating the graph eagerly.

### B2. Streaming embedding ingest

`embeddings.create_best_provider().embed` is sync per-claim. For high-throughput ingest, batch + stream to the embedding model. Doesn't affect retrieval quality, only ingest latency.

### B3. Cross-encoder reranker with retry-tolerant judge

E06 NULL'd because Gemini quota cratered the rerank to 3 calls. If we get an Anthropic key in the bench shell, retry the experiment with Sonnet + tenacity backoff. Possible +0.005 to +0.015 R@5 IF the rerank actually runs on 500/500 questions.

## Sequencing for v3.16.0

1. **S1** (unify weights) — prereq for S3, fixes architectural debt
2. **S2** (RRF-as-boost) — small surface, low risk, parallel to S1
3. **S3** (query-type profiles) — biggest upside, depends on S1
4. **A1** (QA judge run) — once ANTHROPIC_API_KEY available
5. Stop and ship as v3.16.0

A2 (LongMemEval-M) and A3 (trigram) → v3.17.0+.

## Stop conditions for v3.16.0

Same as v3.15.0 plan: 3 consecutive |Δ R@5| < 0.005 → ship what we have. Honest-null acceptance. If S1+S2+S3 produce R@5 ≥ 0.97 — that's a meaningful jump worth a release.
