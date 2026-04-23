# MemPalace-style verbatim recall — 30-prompt eval (2026-04-23)

## TL;DR

Added a third candidate stream (`memorymaster.verbatim_recall`) that runs
FTS5 against the existing `verbatim_memories` corpus (9.4M rows) and mixes
hits into the recall pipeline. **Gated off by default** via
`MEMORYMASTER_RECALL_VERBATIM=1`. Shipped weight `W_VERBATIM=0.0`.

On the 30-prompt `artifacts/real-prompts.jsonl` eval against the live DB:

| metric                         | baseline | verbatim on (W=0.2) | delta    |
|-------------------------------|---------:|--------------------:|---------:|
| **precision@5**               |  0.307   |           **0.320** | **+0.013** |
| **MAP@5**                     |  0.470   |           **0.517** | **+0.047** |
| prompts with ≥1 hit in top-5  |  17/30   |         **18/30**   | **+1**     |
| non-empty rate                | 29/30    |           29/30     |  0         |
| mean candidates / prompt      | 18.6     |           18.7      | +0.1       |

**Result: case (b) — "verbatim doesn't rescue zero-hits but does boost p@5."**

## Verbatim store survey

`memorymaster/verbatim_store.py` stores one row per conversation turn
(user or assistant message ≥20 chars). Schema:

```sql
CREATE TABLE verbatim_memories (
    id INTEGER PRIMARY KEY,
    session_id TEXT,
    role TEXT,
    content TEXT,
    scope TEXT DEFAULT 'project',
    timestamp TEXT,
    source_agent TEXT,
    embedding_synced INTEGER,
    created_at TEXT
);
CREATE VIRTUAL TABLE verbatim_fts USING fts5(
    content,
    content='verbatim_memories', content_rowid='id',
    tokenize='porter unicode61'
);
```

No FK to claims. Dedup by sha256 of content prefix per session.

On live DB (`memorymaster.db`):

| stat                     | value |
|--------------------------|------:|
| total rows               | 9,415,462 |
| distinct scopes          | 40 |
| distinct sessions        | 81 |
| avg content length       | 403 chars |
| max content length       | 37,253 chars |
| min content length       | 20 chars |

Scope distribution is heavily skewed: `project:omniclaude` has 8.83M rows
(93.8% of corpus — bulk transcript imports). `project:wezbridge` 267K,
`project:pauol` 243K, `project:memorymaster` 7.7K.

## Implementation

### New module: `memorymaster/verbatim_recall.py`

- `recall_verbatim(query, scope, db_path, limit=5)` — tokenize via
  `recall_tokenizer._candidate_tokens` (public tokenizer, no internal
  touching), AND-join up to 6 tokens inside quoted FTS5 MATCH, return
  `list[VerbatimHit]`.
- `is_enabled()` — reads `MEMORYMASTER_RECALL_VERBATIM` env.
- `verbatim_weight()` — reads `MEMORYMASTER_RECALL_W_VERBATIM` env
  (default 0.0).
- `hit_to_synthetic_row()` — converts a `VerbatimHit` into a
  query_rows-shaped dict with a fabricated `Claim` object (negative id,
  `claim_type='verbatim'`, `source='verbatim'`, `verbatim_score` set).

### Wire-up: `memorymaster/context_hook.py`

After the entity fanout stage and before the Qdrant fallback, when
`MEMORYMASTER_RECALL_VERBATIM=1`:

1. Call `recall_verbatim(query, scope=None, db_path, limit=5)`.
2. For each hit: if `hit.scope` matches any claim already in the
   candidate pool, BOOST that claim's `verbatim_score` (take max across
   duplicate hits). Otherwise inject as a synthetic candidate, deduped
   by `excerpt[:100]`.
3. Ranker reads `verbatim_score * W_VERBATIM` alongside the existing
   8-dim score.

The boost-or-inject strategy avoids phantom candidates when the same
context is already represented as a curated claim, and gives the
ranker a verbatim-provenance signal it can weight.

### Default weights unchanged

`_RECALL_WEIGHT_DEFAULTS["W_VERBATIM"] = 0.0` — stream is off, weight is
0. Legacy callers get bit-identical ranking to pre-verbatim.

## Measurement

Ran `scripts/eval_recall_precision_at_5.py` in three conditions:

```bash
# Baseline
env -u MEMORYMASTER_RECALL_VERBATIM -u MEMORYMASTER_RECALL_W_VERBATIM \
    python scripts/eval_recall_precision_at_5.py

# Stream ON, weight = 0.2
MEMORYMASTER_RECALL_VERBATIM=1 MEMORYMASTER_RECALL_W_VERBATIM=0.2 \
    python scripts/eval_recall_precision_at_5.py

# Stream ON, weight = 0.0 (isolates pool-widening from reranking)
MEMORYMASTER_RECALL_VERBATIM=1 MEMORYMASTER_RECALL_W_VERBATIM=0.0 \
    python scripts/eval_recall_precision_at_5.py
```

Results:

| condition                              | p@5    | MAP@5  | hits |
|----------------------------------------|-------:|-------:|-----:|
| baseline                               | 0.307  | 0.470  | 17/30 |
| verbatim on, W=0.2                     | 0.320  | 0.517  | 18/30 |
| verbatim on, W=0.0 (pool widen only)   | 0.320  | 0.517  | 18/30 |

**Key finding: W_VERBATIM is load-bearing for ZERO score ... yet the gain
still materialises.** That's because BM25 (shipped on by default) reads
the verbatim rows' `claim.text` (their excerpt), and since the excerpt
IS the conversation turn, it lexically overlaps the prompt and scores
naturally. W_VERBATIM is redundant with BM25 on this corpus.

Swept W_VERBATIM in `{0.05, 0.1, 0.2, 0.3, 0.5, 1.0}` — all flat at
p@5=0.320, MAP@5=0.517, 18/30. No weight beats plain pool-widening.

## Non-empty rate: #25 not rescued

Non-empty rate stayed at 29/30. The surviving zero-hit is prompt #25
*"Continue from where you left off."* — every token is a stopword per
`recall_tokenizer._STOP`, so `_build_match_expr` returns `""` and
verbatim never fires. This matches the ground-truth diagnosis in
`artifacts/recall-zero-hit-prompts-2026-04-23.md` (#25 = "pure
session-resume boilerplate. Ground-truth empty.").

No tokenizer change is going to fix #25 without fabricating matches.

## Honest verdict

**Marginally worth keeping, opt-in only.** Three axes:

1. **Cost:** 9.4M-row FTS5 MATCH with 6-token AND clause — ~5-30 ms per
   call depending on scope-skew. Ingest path is unchanged (stop-hook
   already writes verbatim).
2. **Quality:** +4.3% relative p@5 (0.307 → 0.320), +10% MAP@5, +1
   prompt hitting top-5. Small but real. No regressions.
3. **Redundancy with BM25:** The gain is **100% from candidate pool
   widening** — the W_VERBATIM weight adds zero once BM25 is on. If
   BM25 were disabled (`MEMORYMASTER_LEXICAL_BM25=0`), W_VERBATIM would
   matter, but BM25 ships on.

Shipping recommendation: **keep the module, keep default off.** Power
users with large transcript corpora (→`project:omniclaude` at 8.8M rows)
can flip `MEMORYMASTER_RECALL_VERBATIM=1` and get +0.013 p@5 at a
sub-30ms latency cost. Flipping the default to on would regress the
"ship a tight, behaviour-preserving change" principle — we'd need more
evidence (e.g. a 100-prompt eval showing the gain is stable, not a
30-prompt artefact) before flipping globally.

**NOT redundant with FTS5 claims:** the gain is real. But the gain on
this eval is small enough that the hypothesis "verbatim is a secondary
retrieval surface" holds — it's a widening step, not a game-changer.
The Karpathy/Farza wiki-compilation layer captures the important stuff
anyway.

## Files changed

- `memorymaster/verbatim_recall.py` (new, ~290 lines)
- `memorymaster/context_hook.py` (+72 lines: weight default, stream,
  ranker term)
- `scripts/eval_recall_precision_at_5.py` (+35 lines: verbatim wire-up
  in `_fetch_candidates` and `_score`)
- `scripts/eval_recall_quality.py` (+21 lines: rescue-path verbatim hit)
- `scripts/eval_verbatim_recall.py` (new, 3-condition comparison
  harness)
- `tests/test_verbatim_recall.py` (new, 21 tests)
- `artifacts/verbatim-recall-eval-2026-04-23.md` (this file)

All existing tests: **1336 passed, 39 skipped, 1 xfailed (pre-existing
flaky test_operator)**. No regressions.

## Not shipping

Default `W_VERBATIM=0.0` and `MEMORYMASTER_RECALL_VERBATIM=0` means
production behaviour is unchanged. Flip the env vars to activate.
Branch kept on `omni/feat-verbatim-recall-2026-04-23`, **NOT merged**
per task instructions.
