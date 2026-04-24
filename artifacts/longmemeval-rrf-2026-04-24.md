# LongMemEval — RRF vs linear fusion, 2026-04-24

**Task:** 6.2 RRF vs linear fusion comparison on LongMemEval.
**Harness:** `scripts/run_longmemeval.py` (with new `--output-dir`,
`--config-label`, `--questions` flags plus an RRF mirror in
`recall_with_ranked_ids`).
**Dataset:** `longmemeval_oracle.json` (500 questions, identical to the
6.1 run at `artifacts/longmemeval-2026-04-24.md`).
**Branch:** `omni/feat-longmemeval-rrf-2026-04-24`.
**Base:** `main @ 1c7f41a` — first commit where RRF fusion is actually
shipped in `memorymaster/recall_fusion.py` and `context_hook.py`.

## Why this re-run

6.1's "D + RRF" row was bogus: 6.1 branched off pre-RRF main (`3a34b2d`),
so `MEMORYMASTER_RECALL_FUSION=rrf` had no effect. On top of that, the
harness's private mirror of `context_hook.recall()` only implemented the
linear-sum ranker — setting the env var wouldn't have reached the
ranker even on current main. Fixed both in this branch:

1. `scripts/run_longmemeval.py` now mirrors `context_hook.py`'s RRF
   fusion branch, delegating to the real `memorymaster.recall_fusion`
   module so ranking stays bit-identical to production.
2. Added opt-in `--output-dir` / `--config-label` / `--questions` flags
   so a single labeled config can run while inheriting the caller's
   `MEMORYMASTER_RECALL_*` env. Default multi-config sweep path is
   untouched.

Same 500-question oracle, same seeding path, same scope convention as
6.1 — just re-run under `MEMORYMASTER_RECALL_FUSION=linear` and
`MEMORYMASTER_RECALL_FUSION=rrf` respectively.

## Results (500 questions)

| Config | hit@1 | hit@5 | MRR | mean latency | total runtime |
|---|---:|---:|---:|---:|---:|
| **linear** (default) | **0.342** | **0.430** | **0.3769** | 73.7 ms | 155.8 s |
| **rrf** | **0.404** | **0.440** | **0.4202** | 84.5 ms | 187.3 s |
| **delta** | **+0.062** | **+0.010** | **+0.0433** | +10.8 ms | +31.5 s |
| **rel change** | **+18.1 %** | **+2.3 %** | **+11.5 %** | +14.6 % | +20.2 % |

### Matches 6.1 baseline

The linear row reproduces 6.1 config A exactly: 0.342 / 0.430 / 0.3769.
Same harness invariants, different invocation path — the mirror is
faithful.

### RRF is net-positive on LongMemEval

- **+62 more top-1 hits** (171 → 202 out of 500) — driven almost
  entirely by RRF re-ordering rows the linear ranker already pulled in.
- **+5 more top-5 hits** (215 → 220) — marginal on hit@5, dominant on
  hit@1 and MRR.
- Head-to-head per-question: RRF beats linear on top-1 on **32** Qs,
  linear beats RRF on **1** Q, both agree (or both miss) on **467**.
  On hit@5, RRF wins **5** Qs, linear wins **0**.
- Latency cost is +10.8 ms/query (~15 %), a worthwhile price for
  +18 % hit@1. RRF does O(streams × rows) extra work building each
  stream's ranking — cheaper than another fanout, more expensive than
  the default linear sum.

## Per-question-type breakdown

| Question type | n | linear h@1 | rrf h@1 | Δ | linear h@5 | rrf h@5 | Δ | linear MRR | rrf MRR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| knowledge-update | 78 | 0.462 | 0.500 | +0.038 | 0.526 | 0.526 | 0.000 | 0.491 | 0.513 |
| multi-session | 133 | 0.301 | 0.406 | **+0.105** | 0.451 | 0.481 | **+0.030** | 0.357 | 0.439 |
| single-session-assistant | 56 | 0.357 | 0.357 | 0.000 | 0.357 | 0.357 | 0.000 | 0.357 | 0.357 |
| single-session-preference | 30 | 0.100 | 0.200 | **+0.100** | 0.200 | 0.200 | 0.000 | 0.142 | 0.200 |
| single-session-user | 70 | 0.357 | 0.443 | +0.086 | 0.471 | 0.471 | 0.000 | 0.399 | 0.457 |
| temporal-reasoning | 133 | 0.353 | 0.391 | +0.038 | 0.414 | 0.421 | +0.008 | 0.380 | 0.404 |

RRF ties or wins on every type. The two biggest hit@1 gainers —
`multi-session` (+0.105) and `single-session-preference` (+0.100) —
are precisely the buckets 6.1 flagged as hardest for BM25. RRF pulls
them closer to the strong types without dragging `knowledge-update`
down. `single-session-assistant` is flat because its claims
typically match on a single FTS5 token and all streams agree on the
ranking — nothing for RRF to merge.

## Why RRF works here (and not on the 30-prompt eval)

**Claim 11881** — on a real-DB 30-prompt eval, RRF regressed
p@5 `0.313 → 0.127` and MAP@5 `0.473 → 0.159`. That finding stands for
that corpus. The difference with LongMemEval:

| | 30-prompt live-DB (claim 11881) | LongMemEval 500-Q |
|---|---|---|
| Active streams | only 3 of 8 populated | 2 — BM25 + freshness (no vector/entity/verbatim) |
| Active per-query signals | 2/8 ranker signals were query-overlap (phrase / matches / all_present) and **can't** feed RRF | BM25 dominates; freshness adds a secondary ranking |
| Candidate pool depth | sparse — few claims per query | dense — per-token fanout pulls 8+ rows even on short queries |
| Consequence of RRF | streams disagree, and the query-overlap boosters that drove the linear ranker are silently dropped — RRF collapses to noisy BM25 alone | BM25 + freshness each nominate a distinct top-K, RRF picks the consensus |

Put another way: RRF shines when **several independent rankings agree on
the same documents**. On LongMemEval we have a BM25 ranking AND a
freshness ranking with enough recall-depth that the two streams overlap
on the golden session; RRF's dampening of the single-stream head stops
BM25 from getting steamrolled by cross-question tokens. On the live 30-
prompt set, the query-overlap booster signals (`matches`, `phrase`,
`all_present`) are load-bearing for the linear ranker but fall outside
the current RRF stream set — RRF ends up strictly weaker than linear
because it throws away signal.

So: **the two results are consistent once you hold "what streams are
actually populated" constant.** RRF needs ≥2 meaningfully-populated
independent streams; LongMemEval has that (BM25 + freshness on dense
seeding), the 30-prompt set does not.

## 5 sample questions where linear and RRF give different top-1s

### 1. `2c63a862` (temporal-reasoning) — **RRF wins hit@1**
- **Q:** "How many days did it take for me to find a house I loved after starting to work with Rachel?"
- **golden:** `[answer_d39b7977_1, answer_d39b7977_2]`
- **linear top-1:** `answer_1c6b85ea_1` (from question `gpt4_2487a7cb`'s corpus — cross-Q leak)
- **rrf top-1:** `answer_d39b7977_2` (correct session)

### 2. `gpt4_2f56ae70` (temporal-reasoning) — **RRF wins hit@1**
- **Q:** "Which streaming service did I start using most recently?"
- **golden:** `[answer_7a36e820_2, answer_7a36e820_1, answer_7a36e820_3]`
- **linear top-1:** `answer_da704e79_1` (from `gpt4_d9af6064`)
- **rrf top-1:** `answer_7a36e820_2` (correct)

### 3. `gpt4_2f584639` (temporal-reasoning) — **RRF wins hit@1**
- **Q:** "Which gift did I buy first, the necklace for my sister or the photo album for my mom?"
- **golden:** `[answer_11a8f823_2, answer_11a8f823_1]`
- **linear top-1:** `answer_016f6bd4_1` (from `a3045048`)
- **rrf top-1:** `answer_11a8f823_1` (correct)

### 4. `c4ea545c` (knowledge-update) — **linear wins hit@1**
- **Q:** "Do I go to the gym more frequently than I did previously?"
- **golden:** `[answer_d3bf812b_1, answer_d3bf812b_2]`
- **linear top-1:** `answer_d3bf812b_1` (correct)
- **rrf top-1:** `answer_1de862d6_3` (from `129d1232`'s corpus — cross-Q leak)
- This is the ONLY question across all 500 where RRF strictly loses hit@1 to linear. The gym/frequency tokens are so common across the corpus that freshness (RRF's second stream) votes for a more recent, unrelated gym mention, and RRF's consensus overrides BM25's correct pick.

### 5. `gpt4_93159ced` (temporal-reasoning) — **both miss, different top-1**
- **Q:** "How long have I been working before I started my current job at NovaTech?"
- **golden:** `[answer_e5131a1b_1, answer_e5131a1b_2]`
- **linear top-1:** `answer_4be1b6b4_1` (from `gpt4_2655b836`)
- **rrf top-1:** `answer_4ffa04a2_5` (from `a3838d2b`)
- Both wrong. The word "NovaTech" is in the question but not dominant enough in any claim chunk to survive the 500-way corpus mix. Classic cross-question contamination — no fusion strategy can salvage a query where the golden session wasn't in the top-8 fanout to begin with.

## Verdict

**RRF is net-positive on LongMemEval, neutral-to-positive on every
question type.** Ship it as the default on this workload. The headline:
`hit@1 0.342 → 0.404, MRR 0.377 → 0.420`, at a ~15 % latency cost and
one regression out of 500 Qs.

**RRF and the 30-prompt null (claim 11881) are NOT in contradiction** —
they measure RRF on different stream topologies:

- LongMemEval: 2 meaningful streams (BM25 + freshness), dense candidate
  pool, RRF helps (+18 % hit@1).
- 30-prompt live-DB: 3 populated streams but 2/8 ranker signals are
  query-overlap scores that never enter any RRF stream, sparse
  candidate pool, RRF hurts (−0.314 MAP@5).

Both are real. The right gate before promoting RRF to default is a
**topology check** — count "streams with ≥k populated rows where `all
scores == 0` is false." If ≥2, use RRF; if <2, fall back to linear.
That's a one-line change in `context_hook.py`; not in scope for this
task (don't-modify-memorymaster rule), but flagged as the obvious
follow-up for anyone who reads this artifact.

## Cross-question contamination (optional investigation)

The harness currently seeds all 500 questions into one shared
`bench-<label>.db`, so BM25 can retrieve claims from OTHER questions
that share tokens with the current prompt. 6.1 identified this as the
dominant failure mode: 100 % of hit@5 misses had top-1 from a
different question's corpus.

Looking at `run_longmemeval.py`: no flag exists to toggle per-question
DB isolation. Adding one would be a non-trivial restructure of
`run_worker` (swap `bench-<config>.db` persistence for a tear-down
loop per question, plus re-init the `MemoryService` each iteration)
and is explicitly called out as out-of-scope for this task. Noted as
a missing feature; sample 5 above illustrates the signature — both
linear and RRF miss the same way because neither can suppress
cross-Q leaks at rank time.

## Honest caveats

- Linear ran in **155.8 s**, RRF in **187.3 s** — +20 % total runtime.
  On a daily/weekly benchmark this is trivial; on a hot-path user
  query +11 ms matters more (average +11 ms / query).
- The +0.010 hit@5 delta is small enough that a different random seed
  on the dataset ordering could flip it. The +0.062 hit@1 and +0.0433
  MRR deltas are comfortably outside noise.
- RRF uses the default `k=60` (`RRF_K_DEFAULT` in `recall_fusion.py`).
  Not tuned. A sweep across `k ∈ {30, 60, 120}` is a plausible next
  experiment.
- Vector fallback was NOT populated — `QDRANT_URL` is cleared in the
  worker env and `MEMORYMASTER_RECALL_VECTOR_FALLBACK` is off by
  default. Both fusion modes are therefore evaluated in a "BM25 +
  freshness only" regime. With vectors on, RRF's advantage likely
  grows (more streams to merge).

## Artifacts

- `artifacts/longmemeval-rrf/results-linear.jsonl` — per-question records, linear
- `artifacts/longmemeval-rrf/results-rrf.jsonl` — per-question records, RRF
- `artifacts/longmemeval-rrf/summary-linear.json` — roll-up, linear
- `artifacts/longmemeval-rrf/summary-rrf.json` — roll-up, RRF
- `scripts/run_longmemeval.py` — harness (flags added, no behavior
  change on the default path)
- `artifacts/longmemeval-2026-04-24.md` — 6.1 artifact, preserved as-is
