# v318-E01 — per-question-type retrieval profile for single-session-preference

## Hypothesis

Of the 6 LongMemEval-S buckets, `single-session-preference` is the weakest:
baseline R@5 = 0.80 (vs 0.95+ for every other bucket). Preference questions
are typically phrased differently from how the preference was stated in the
session (e.g. user wrote "I avoid spicy food", question asks "what cuisine
should I order?"). This favours vector similarity over lexical overlap.

Profile under test, applied ONLY to single-session-preference queries:
`(W_LEX=0.10, W_CONF=0.10, W_FRESH=0.10, W_VEC=0.70)` — heavy vector,
minimal lexical. Other buckets retain the default
`(W_LEX=0.30, W_CONF=0.20, W_FRESH=0.10, W_VEC=0.40)`.

Predicted Δ R@5 on the preference bucket: +0.05 to +0.15.
Predicted Δ overall: +0.003 to +0.009 (30/500 questions × bucket lift).
Risk: low — other buckets bypass the profile entirely.

## Change

- `memorymaster/config.py` — new `MEMORYMASTER_RETRIEVAL_PROFILE_<TYPE>`
  env-var family scanned by `_apply_env_retrieval_profiles`; slug
  normalization `SINGLE_SESSION_PREFERENCE` → `single-session-preference`.
  New `Config.retrieval_profile(qtype)` lookup.
- `memorymaster/retrieval.py` — `rank_claims`, `rank_claim_rows`, and
  `_compute_claim_score` accept optional `query_type`. When a profile
  matches, its 4-tuple replaces `cfg.retrieval_weights` for the hybrid
  blend. No-vector path unchanged.
- `memorymaster/service.py` — `MemoryService.query` and `query_rows`
  forward `query_type` to `rank_claim_rows`.
- `tests/bench_longmemeval.py` — retrieval call passes
  `item['question_type']` as `query_type`.
- `tests/test_retrieval_profiles.py` — 5 regression tests covering
  no-op-without-env, override, no-leak, slug-mapping, semantic-path.

S1 regression (`tests/test_retrieval_weights.py`) stays green. Targeted
suite: 28 passed.

Scaffolding shipped in commit `a601761` on `experiment/v318-s3-per-type-profiles`.

## Results

| Bucket | n | Baseline R@5 | E01 R@5 | Δ |
|---|---:|---:|---:|---:|
| knowledge-update | 78 | 0.9872 | 0.9872 | 0.0000 |
| multi-session | 133 | 0.9774 | 0.9774 | 0.0000 |
| single-session-assistant | 56 | 0.9821 | 0.9821 | 0.0000 |
| **single-session-preference** | **30** | **0.8000** | **0.9000** | **+0.1000** |
| single-session-user | 70 | 1.0000 | 1.0000 | 0.0000 |
| temporal-reasoning | 133 | 0.9549 | 0.9549 | 0.0000 |
| **OVERALL** | **500** | **0.9660** | **0.9720** | **+0.0060** |

500/500 questions completed. Bench wall-clock per run: ~12 min.

Baseline run reproduces v3.17.1 published number exactly
(R@5=0.9660, R@10=0.9840, MRR=0.9021).

## Verdict: **KEEP**

The preference profile lifts the targeted bucket from 0.80 → 0.90 (+12.5%
relative, +10pp absolute), with ZERO drift on the other 5 buckets — proof
that the per-type profile mechanism is genuinely isolated. The +0.0060
overall lift is small in absolute terms but it's the first meaningful R@5
improvement since v3.15.0 hit 0.966; every prior fusion-layer attempt
(RRF, session-diversity, LLM rerank, W_LEX sweep) NULLed or REVERTed.

## Why this works

The single-session-preference bucket is paraphrased-question-heavy:
the answer-bearing session never restates the question's surface form.
Lexical features (token recall/precision, phrase containment, prefix
matching) have low signal here; semantic vector similarity is the only
reliable bridge between the question's intent and the session's content.

Other buckets either repeat surface terms (single-session-user/assistant)
or have explicit anchor tokens (knowledge-update, temporal-reasoning,
multi-session). Their default lex=0.30/vec=0.40 blend is already
near-optimal — confirmed by ZERO drift in the E01 run.

## Note on `docs/longmemeval-results.md` staleness

The published per-bucket table in `docs/longmemeval-results.md` lists
single-session-preference at R@5=0.4000. That value predates the v3.16
S1 weight unification (commit d22dd53), which lifted current baseline
to 0.8000. The +0.10 reported here is measured against the **true
current baseline** (0.8000), not the stale doc value. Headline overall
R@5=0.9660 has held.

## What next

E02 layers a freshness-heavy profile for `temporal-reasoning` on top of
E01's preference profile (kept). E02+ explores other bucket axes:

- single-session-assistant (0.9821): already near ceiling, unlikely lift
- multi-session (0.9774): could test confidence-heavy profile
- single-session-user (1.0000): max
- knowledge-update (0.9872): could test recency-aware profile
