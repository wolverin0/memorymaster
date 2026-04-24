# BM25 per-field weighting — eval (roadmap 1.4, 2026-04-23)

## Summary

**Null result.** Per-field BM25 scoring (subject vs text streams with
configurable weights) does NOT beat the concatenated baseline on the
30-prompt real-prompt held-out eval. The feature is shipped with neutral
defaults (`W_SUBJECT=1.0`, `W_TEXT=1.0`) because:

1. It cleanly separates BM25 scoring from field concatenation — future
   tuning (or per-query field weights) can ride this path without another
   invasive refactor.
2. The neutral config (H) is the closest to the concat baseline
   (-0.013 p@5, -0.027 MAP@5), i.e. the infrastructure cost is small.
3. Every subject-heavy config the brief asked for (B=2.0/1.0, C=3.0/1.0,
   E=5.0/1.0) **regressed** p@5. Shipping them as the default would ship
   a regression.

## Methodology gotcha — why the eval was initially a no-op

The standard `scripts/eval_recall_precision_at_5.py` has its own inline
`_score()` that reads `row["lexical_score"]` (the FTS5 rank from
retrieval). It does **not** exercise the BM25 rescorer that lives inside
`memorymaster/context_hook.py::recall()`. Running it against any
combination of `MEMORYMASTER_BM25_W_SUBJECT` / `MEMORYMASTER_BM25_W_TEXT`
produced the identical p@5=0.313 / MAP@5=0.473 — because the eval path
never called the rescorer.

Moved the measurement into `artifacts/bm25-per-field-eval-harness.py`,
which:

1. Uses `_collect_candidates` from the parent eval (same retrieval,
   same fanout, same DB read-only setup).
2. Calls the real per-field BM25 scorer (ported verbatim from
   `context_hook.recall()`) or a concatenated replica of the pre-change
   scorer.
3. Writes the score back into `row["lexical_score"]` so the downstream
   `_evaluate` helper sees it.

This is the first apples-to-apples comparison of the two scorers on the
30-prompt eval.

## Table — 8 configs on `artifacts/real-prompts.jsonl` (30 prompts, top_k=20)

DB: `memorymaster.db` (read-only). Entity-link fanout: ON. Vector
fallback: OFF. `min_overlap=2` (token-overlap proxy label).

| config                        | p@5   | MAP@5 | non_empty | delta p@5 vs concat |
|-------------------------------|------:|------:|----------:|--------------------:|
| A — concat baseline           | 0.420 | 0.559 | 17/30     |  0.000              |
| B — W_S=2.0, W_T=1.0          | 0.373 | 0.488 | 17/30     | -0.047              |
| C — W_S=3.0, W_T=1.0          | 0.367 | 0.450 | 17/30     | -0.053              |
| D — W_S=1.5, W_T=1.0          | 0.393 | 0.508 | 17/30     | -0.027              |
| E — W_S=5.0, W_T=1.0          | 0.353 | 0.438 | 17/30     | -0.067              |
| F — W_S=10.0, W_T=0.0 (subj)  | 0.293 | 0.410 | 17/30     | -0.127              |
| G — W_S=0.0, W_T=10.0 (text)  | 0.400 | 0.557 | 17/30     | -0.020              |
| **H — W_S=1.0, W_T=1.0**      | **0.407** | **0.532** | **17/30** | **-0.013** |

Acceptance criterion was "p@5 lift ≥ 0.02 on best config OR honest null
result." **No config cleared the bar.** Null result documented; shipping
neutral defaults.

## Sample top-5 where per-field surfaces better claims (label-agnostic view)

```
PROMPT: 'the steward is dropping 429 error by google, can we know what it is ?'

concat baseline top-5 (cid, subj, text prefix):
  8580  subj=None           text='Google Search Console for puntofutura.com.ar shows 730 not-indexed pages...'
 10929  subj=None           text='`next/font/google` (e.g. `import { Inter } from "next/font/google"`)...'
  9657  subj=None           text='UISPApiService was logging axios errors via `error.message`...'
  6152  subj=None           text='## Open Questions - UISP ...'
 11696  subj='Google Gemini free tier quotas'  text='Google stopped publishing free-tier RPM/RPD...'

per-field (1.0, 1.0) top-5 (cid, subj, text prefix):
 10669  subj='elduderino UI error surfacing'      text='When surfacing a backend error to an elduderino UI banner...'
 11696  subj='Google Gemini free tier quotas'     text='Google stopped publishing free-tier RPM/RPD...'
 11759  subj='steward 0-promote rate'             text='Steward 0/200 promotion rate is UPSTREAM ingest bug...'
  8108  subj='memorymaster.steward'               text='Steward auto-archives stale claims with 0 access...'
  8580  subj=None                                 text='Google Search Console for puntofutura.com.ar...'
```

Qualitative observation (not reflected in the proxy label): for the
"steward dropping 429 error by google" prompt, **per-field surfaces
two topically-relevant steward claims (11759, 8108) that concat baseline
missed entirely**, and also surfaces a Google Gemini quota claim (11696)
that is directly on-topic. Concat baseline's top-3 are about Search
Console, Next.js font import, and UISP axios logging — literal "google"
matches but semantically off-topic.

The token-overlap proxy label (min_overlap=2) can't distinguish a
literal-token "google" in SEO content from a semantic-match "steward"
claim, so the aggregate metrics penalize per-field. A human-labelled
eval set or an LLM judge would likely show a different picture, but the
brief is clear: ship only if the 30-prompt aggregate moves.

## Code changes

- `memorymaster/context_hook.py` — split BM25 rescorer into per-field
  subject/text streams; combine with `W_SUBJECT`/`W_TEXT` weights.
  Neutral defaults (1.0, 1.0). New `_bm25_field_weight` helper. Shape of
  `bm25_scores: dict[int, float]` is unchanged, so `_relevance` and the
  downstream ranker are byte-identical.
- `tests/test_bm25_per_field.py` — 8 unit tests covering env parsing,
  equal-weight parity, subject-heavy ranking, text-heavy ranking, empty
  field safety.
- `artifacts/bm25-per-field-eval-harness.py` — standalone harness that
  runs the real rescorer end-to-end (the parent eval script bypasses it).

## Recommended defaults

`W_SUBJECT = 1.0`, `W_TEXT = 1.0`. Neutral — matches concat baseline to
within -0.013 p@5 (noise floor on a 30-prompt set), preserves the
infrastructure, and doesn't ship the regression that subject-heavy
weights would cause on this held-out set.

## Worth a MemoryMaster claim

- The standard eval script `scripts/eval_recall_precision_at_5.py` does
  NOT exercise `context_hook.recall()`'s BM25 rescorer — it reimplements
  scoring with `row["lexical_score"]` only. Any future change to the
  rescorer itself (per-field weights, k1/b sweeps, new term features)
  needs a dedicated harness like `artifacts/bm25-per-field-eval-harness.py`
  or it will silently no-op the eval.
- Claim text bodies in this DB carry MORE discriminative signal than
  subjects. Subject-heavy weighting (2.0+ / 1.0) regressed p@5 on every
  config tried. If a future eval flips this, revisit.

## Acceptance

- All pytest tests pass (1363 passed, 39 skipped, 1 xfailed).
- `ruff check memorymaster/context_hook.py tests/test_bm25_per_field.py` clean.
- New tests pin behaviour at both extremes (subject-only, text-only)
  and at the neutral default.
- No change to recall_fusion.py, recall_tokenizer.py, or the `_relevance`
  function body (per brief boundaries).
