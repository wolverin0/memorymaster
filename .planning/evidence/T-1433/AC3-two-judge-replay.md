<!-- doc-head: T-1433 AC3 two-judge replay of index + on-demand reading versus similarity injection -->
<!-- Covers: method, snapshot/prompt provenance, both judges, cost/noise/recall per strategy, intervals, recommendation. -->
<!-- Evidence: 80 real prompts 2026-10-23..30, read-only DB snapshot, Gemini 4 Argon + Claude Sonnet blind judges. -->
<!-- Supersedes the single-judge AC3 section of AC3-comparison.md (merged in PR #273). -->
<!-- /doc-head -->
# T-1433 AC3: index + on-demand reading vs similarity injection (two blind judges)

Measured 2026-10-30 at source `73e71c1` with `scripts/eval_index_vs_similarity.py`.
It replaces the AC3 comparison in `AC3-comparison.md` (PR #273, merged), which used
one GPT judge and held the candidate pool to the two injected claims.

**Result.** Over 79 real prompts, index + on-demand reading (B) cost **1,547
characters per prompt against 2,154** for similarity injection (A, −28%). It put
**0.59 irrelevant bodies per prompt into context against 1.20** (−51%, consensus
labels). Relevant recall was **57% against 34%** under consensus and Gemini labels,
and **41% against 35%** under Claude labels. The cost and noise intervals exclude
zero under all three label sets. The recall gain excludes zero under consensus and
Gemini labels, but not under Claude labels.

**Recommendation: build B as an opt-in pilot and keep A as the default.** The pilot
injects a top-8 index and exposes a read-by-id tool. Change the default only after a
live pilot shows that real agents call the read tool about as often as the
instructed reader here (56 of 79 prompts), and that the read round-trip's latency
is acceptable. The larger problem is upstream: only 44 of 864 pool candidates (5.1%)
are consensus-relevant, and 93% of index titles are irrelevant. Retrieval
precision, through the threshold and claim titles, matters more than the delivery
format.

## Method

- **Snapshot.** The claims DB was copied with the sqlite3 backup API from a
  `mode=ro` connection, about 22:20 UTC on 2026-10-30. Everything was read from the
  copy, opened `read_only=True`. The evaluator never calls `_finish_recall`, so it
  spools no access record, writes no session state and appends no telemetry line.
  Candidate IDs came from the running dense service (EmbeddingGemma 2,
  `indexed=7472` for every prompt), which was only queried.
- **Prompts.** Real top-level prompts were taken from the Claude Code transcripts
  for 2026-10-23 00:00 to 2026-10-30 00:00 UTC, through the parsers in
  `scripts/review_recall_citations.py`. The verbatim store was not used because its
  `verbatim_memories` user turns stop on 2026-08-24, so it has no recent prompts.
  `transcripts.db` holds conversation fragments, not individual prompts.
- **Exclusions.** The hook's own gates were applied first: `is_automated` removed
  841 prompts, `is_trivial_prompt` 2, `disabled_for(cwd)` 114 and empty prompts 1.
  The sensitivity scan removed 33, and 927 duplicate texts (resumed copies) were
  dropped. That left 1,022 eligible prompts.
- **Sampling.** The first 80 eligible prompts were taken in
  `sha256("T-1433-ac3:20261030:" + uuid)` order. The private manifest (`uuid`, `ts`,
  `cwd`) has SHA-256 `e9b9ab55e2335db6c5bd392a08fa0f56fa385d2bc57ae03140fa4c5de85c93cf`.
  One prompt had an empty pool, so both strategies spend 0 on it. It is left out
  of the 79 scored events.
- **Pool.** The pool was built as in production: scope from the prompt's `cwd`,
  then `_prompt_recall_plan`, `_dense_prompt_rows` and `apply_truth_eligibility`.
  Thresholds were unchanged (in-scope 0.68, outside 0.76). The only change was a
  cap of 12 (`FETCH_K`) instead of 2, so the pool holds every authorized candidate
  above threshold: 864 candidates, 61 of 80 pools full.
- **A.** The first `hook_policy.max_claims()` = 2 pool rows, rendered by
  `_render_recall_lines`/`_recall_text` exactly as the hook renders them. Cost is
  `len(block)`, which is the telemetry `chars` field.
- **B.** An index (`[Memory index; read a claim body by id only when needed]` plus
  `- [id] <first 72 collapsed chars>`) of the first 8 pool rows, the same title
  rule as PR #273. A reader (Claude Haiku, `claude -p`, no tools) saw only the
  prompt and the index and picked 0 to 3 IDs. Its choices were frozen before any
  judging. Cost is the index plus every body requested, full text, including wrong
  reads.
- **Judges.** Gemini 4 Argon (medium) ran through the repo's `AntigravityClient`,
  and Claude Sonnet through `claude -p` with no tools. Each judge saw the prompt
  and every pool body in a seeded shuffle, labelled C1..Cn, with no IDs, rank or
  strategy. Both labelled every candidate R/I/U with the PR #273 rubric. Both
  strategies are scored against the same labels. Consensus counts a label only
  when both judges agree. U and disagreements stay out of the relevance
  denominators.
- **Hook isolation.** The `claude -p` calls ran with `--setting-sources project`
  from an empty scratch directory, so the operator's installed hooks did not run.
  The recall hook's start count did not change across the 160 calls. A first pass
  without that flag let the UserPromptSubmit recall hook fire on every
  reader/judge call. That hook put live claims into the reader's context for free,
  so the reader read less (88 bodies, against 118 here). It also wrote hook state.
  That pass was discarded and rerun. The Gemini judge runs in the Antigravity
  batch profile, which is already hook-isolated.
- **Privacy.** Home paths and private IPv4 addresses were masked in everything sent
  to a model. Prompt and claim text stayed in the session scratchpad. This file
  publishes only ordinals, positions, labels and counts.
- **Transport notes.** OpenCode's OpenAI route returned 401, its Google route had
  no API quota, and DeepSeek hit its weekly cap. A GPT-family judge was therefore
  not available.

## Results (79 events, 864 candidates)

| Measure | Consensus | Gemini 4 Argon | Claude Sonnet |
|---|---:|---:|---:|
| Pool candidates R / I / other | 44 / 678 / 142 | 47 / 775 / 42 | 142 / 705 / 17 |
| A chars per prompt | 2,154.47 | same | same |
| B chars per prompt (index + reads) | 1,547.06 (704.03 + 843.04) | same | same |
| A irrelevant bodies / judged bodies | 95/110 = **86.4%** | 130/146 = 89.0% | 102/152 = 67.1% |
| B irrelevant reads / judged reads | 47/72 = **65.3%** | 80/107 = 74.8% | 54/112 = 48.2% |
| B irrelevant index titles / judged titles | 452/487 = 92.8% | 531/569 = 93.3% | 474/588 = 80.6% |
| Irrelevant bodies per prompt, A to B | **1.203 to 0.595** | 1.646 to 1.013 | 1.291 to 0.684 |
| Relevant recall vs pool, A | 15/44 = **34.1%** | 16/47 = 34.0% | 50/142 = 35.2% |
| Relevant recall vs pool, B | 25/44 = **56.8%** | 27/47 = 57.4% | 58/142 = 40.8% |
| Relevant claims reachable in B's index | 35/44 | 38/47 | 114/142 |
| Prompts with a relevant body / prompts with one in pool, A | 13/22 = 59.1% | 14/23 = 60.9% | 37/53 = 69.8% |
| Same, B | 18/22 = 81.8% | 19/23 = 82.6% | 38/53 = 71.7% |

Totals: A 170,203 characters with 158 bodies. B used 55,618 index plus 66,600 read
characters, with 118 bodies read on 56 of 79 prompts.

Paired bootstrap of B − A (2,000 resamples of prompts, seeded, 95% percentile):

| Label set | Chars per prompt | Irrelevant bodies per prompt | Relevant recall |
|---|---|---|---|
| Consensus | [−1,046.3, −201.3] | [−0.823, −0.392] | [+0.061, +0.405] |
| Gemini | [−1,048.0, −193.0] | [−0.873, −0.392] | [+0.054, +0.400] |
| Claude | [−1,004.2, −211.2] | [−0.810, −0.392] | [−0.050, +0.154] |

Judge agreement on R versus not-R over 864 candidates: raw 88.3%, Cohen's kappa
**0.42** (moderate). Claude Sonnet marks about three times as many candidates R,
and most disagreements are Claude-R / Gemini-I. The size of the recall gain
therefore depends on the judge, while cost and noise favour B under every label
set.

## Limits

- **Fresh-session replay.** Session deduplication was not replayed. Production
  dense telemetry for the same week averages 1,395 characters and 1.21 claims per
  prompt, and 37.5% of prompts get no block. The replay averages 2,154 characters
  and 1.98 claims, with 1 of 80 empty. Absolute costs here are fresh-session costs,
  and B would get the same deduplication. The lexical fallback did not run because
  the dense service answered every prompt. The transcript fallback is outside this
  claims-only comparison.
- **Reader.** The reader was an instructed Claude Haiku, and production agents may
  read more or less often. Read-tool latency and tool-call overhead are not counted.
- **Family overlap.** The reader and one judge are both Claude, and the Claude judge
  shows the smallest recall gain. Gemini labels give the family-independent view.
- **Pool-relative recall.** "Relevant recall" counts only the authorized pool of up
  to 12 candidates. Relevant claims below the threshold or outside the dense index
  are not counted.
- **Short-ID collisions.** Short IDs are not unique across scopes. Five pools held
  two to four claims with the same short ID. That ID is shared by six claims
  DB-wide; four of them, from four scopes, met in one pool through the
  outside-scope path. The evaluator renames them `<id>~<claim id>` so that reads
  resolve. A production read-by-id tool must resolve the same ambiguity.

## Reproduce

```
python scripts/eval_index_vs_similarity.py collect --out-dir OUT --since 2026-10-23 --until 2026-10-30 --n 80
python scripts/eval_index_vs_similarity.py pool    --out-dir OUT --snapshot SNAPSHOT.db
python scripts/eval_index_vs_similarity.py read    --out-dir OUT
python scripts/eval_index_vs_similarity.py judge   --out-dir OUT
python scripts/eval_index_vs_similarity.py report  --out-dir OUT
```

Every step resumes from the files in `OUT`, and failed model calls are retried on
the next run, never scored as empty. The prompt sample is deterministic, but model
labels and reads are not bit-reproducible.

## Anonymized ledger

`Pool` is the number of candidates in retrieval order. `A` and `Read` are positions
in that order: B's index covers positions 1 to 8. The label strings follow pool
order.

| Sample | Pool | A | Read | Gemini labels | Claude labels | A chars | Index chars | Read chars |
|---|---:|---|---|---|---|---:|---:|---:|
| S01 | 12 | 1,2 | 3,2,4 | IIIIIIIIIIII | IIIIIIIIIIII | 3832 | 736 | 510 |
| S02 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIIII | 3843 | 740 | 0 |
| S03 | 12 | 1,2 | 1,5 | IIIIIIIIIIII | IIIIRRRRIIRI | 412 | 736 | 334 |
| S04 | 12 | 1,2 | 1,5,7 | RIIIIIIIIIII | RIIIIIIIIIRI | 457 | 752 | 934 |
| S05 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIIII | 3372 | 746 | 0 |
| S06 | 12 | 1,2 | 1,2 | IIIIIIIIIIII | RIIIIIIIIRIR | 390 | 736 | 311 |
| S07 | 12 | 1,2 | 2,1 | IRIIIIIIIIII | IRIIRIIIIIII | 458 | 759 | 379 |
| S08 | 11 | 1,2 | - | IIIIIIIIIII | IIIIIIIIIII | 4946 | 740 | 0 |
| S09 | 12 | 1,2 | 2,1,8 | IIIIIIIIIIII | RIIIIIIIIIII | 1495 | 738 | 2113 |
| S10 | 7 | 1,2 | 2,1 | RIIIIII | IRIIIII | 4771 | 653 | 4692 |
| S11 | 12 | 1,2 | 3,6 | IIRRRRIIIIRI | IIIRRRIIIIRI | 1687 | 740 | 3796 |
| S12 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIIII | 6467 | 738 | 0 |
| S13 | 12 | 1,2 | 1,7 | RIIIIIIIIIIR | RIIIIIIIIIIR | 1977 | 736 | 2329 |
| S14 | 12 | 1,2 | 7,8,5 | IIIIIIRRRIII | IIIRRIRRRRII | 441 | 738 | 1409 |
| S15 | 5 | 1,2 | 2,4,3 | IIIRI | IRIRI | 3816 | 486 | 781 |
| S16 | 2 | 1,2 | - | II | II | 4657 | 226 | 0 |
| S17 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIIII | 5100 | 736 | 0 |
| S18 | 12 | 1,2 | 2,1 | IIIIIIIIIIII | IIIIIIIIIIII | 1748 | 742 | 1669 |
| S19 | 12 | 1,2 | 3,5,2 | IIIIIIIIIIII | IIRIIIIIIIII | 322 | 736 | 479 |
| S20 | 12 | 1,2 | - | IIIIIIIIIIII | RIIIIIIIIIII | 369 | 738 | 0 |
| S21 | 12 | 1,2 | 8,7 | IIIIIIIIIIII | IIIIIIIIIIII | 4509 | 736 | 947 |
| S22 | 12 | 1,2 | 2,3 | UUUUUUUUUUUU | UUUUUUUUUUUU | 249 | 685 | 234 |
| S23 | 12 | 1,2 | 2 | IIIIIIIIIIII | RRRRRRIRIIII | 1316 | 736 | 221 |
| S24 | 10 | 1,2 | - | IRIRIIIIRI | IRIRRIIIRR | 3900 | 736 | 0 |
| S25 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIIII | 3786 | 740 | 0 |
| S26 | 12 | 1,2 | 2,3 | IIIIIIIIIIII | IIIIIIIIIIII | 3420 | 740 | 3786 |
| S27 | 12 | 1,2 | 1,2,6 | RIIIIIIIRIII | RIRIIRIIRIII | 458 | 750 | 576 |
| S28 | 9 | 1,2 | 2,1 | IIIIIIIII | RRIIIIIII | 515 | 736 | 436 |
| S29 | 12 | 1,2 | 1,2,7 | RRIIIRIRIIIR | RRIIIRRRIIIR | 448 | 736 | 461 |
| S30 | 12 | 1,2 | 1 | IIIIIIIIIIII | IIIIIIIIIIII | 2300 | 738 | 1670 |
| S31 | 3 | 1,2 | 1,2 | UUU | UUU | 1124 | 313 | 1045 |
| S32 | 12 | 1,2 | 7 | IIIIIIRIIIII | IIIRRIRIIIRI | 5540 | 736 | 1838 |
| S33 | 12 | 1,2 | 6 | IIIIIIIIIIII | IIIIIIIIIRII | 2500 | 736 | 1426 |
| S34 | 4 | 1,2 | 1 | IIII | RIRI | 439 | 396 | 159 |
| S35 | 12 | 1,2 | 5,4 | IIIRIIIIIIII | IRIRIIIIRIRI | 5036 | 736 | 1747 |
| S36 | 12 | 1,2 | 2,5 | UUUUUUUUUUUU | IIIIIIIIIIIR | 400 | 736 | 325 |
| S37 | 12 | 1,2 | - | IIIIIIIIIIII | RRIIIIIIIIII | 1545 | 736 | 0 |
| S38 | 12 | 1,2 | 8,6 | IIIIIIIIIIII | IIIIIIIIIIII | 2924 | 736 | 548 |
| S39 | 12 | 1,2 | 1,2,4 | RRRRIIIIIIRI | RRIRIIIIIIRI | 2043 | 738 | 2603 |
| S40 | 8 | 1,2 | - | IIIIIIII | IIIIIIII | 447 | 713 | 0 |
| S41 | 12 | 1,2 | 8 | IIIIIIIIIIII | IIIIIIIIIIII | 3153 | 736 | 1076 |
| S42 | 9 | 1,2 | 1,7 | RIIIIIIII | RRIIIIIRI | 2280 | 736 | 1836 |
| S43 | 12 | 1,2 | 7,5 | IIIIIIIIIIII | RIIIIIRIIIII | 424 | 736 | 341 |
| S44 | 9 | 1,2 | - | IIIIIIIII | RRIRRRIII | 4041 | 736 | 0 |
| S45 | 12 | 1,2 | 1,4,8 | RIIRRIIRRIII | RIIRRIIRRIII | 366 | 736 | 507 |
| S46 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIIII | 3847 | 736 | 0 |
| S47 | 12 | 1,2 | 1,2,5 | IIIIIIRIIIII | RRRIRIRRRRII | 393 | 736 | 457 |
| S48 | 12 | 1,2 | 1 | IIIIIIIIIIII | RIIIIIIIIIII | 2628 | 748 | 193 |
| S49 | 12 | 1,2 | 4,3,1 | IIIIIIIIIIII | RRRRIIIRIIII | 427 | 738 | 601 |
| S50 | 12 | 1,2 | 3,4 | IIIIIIIIIIII | IIIIIIRIIIII | 4946 | 736 | 359 |
| S51 | 12 | 1,2 | 3,2 | IIRIIIIIIIII | RRRIIRIIIIII | 2039 | 736 | 1927 |
| S52 | 12 | 1,2 | 6,5,7 | IIIIIIIIIIIR | IIIIIIIIIIIR | 4436 | 738 | 2545 |
| S53 | 12 | 1,2 | 3,2 | RIRIIIIIIIII | RIRIIIIIIIRI | 1446 | 729 | 777 |
| S54 | 12 | 1,2 | 4,7 | IIIRIIRIIIII | IIIRIIRIIIII | 451 | 736 | 1028 |
| S55 | 11 | 1,2 | 3,4 | IIIIIIIIIII | IIRRIIIIIII | 4320 | 740 | 326 |
| S56 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIIII | 295 | 740 | 0 |
| S57 | 10 | 1,2 | 6,7 | UUUUUUUUUU | IIIIIIIIII | 274 | 744 | 218 |
| S58 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIIII | 1647 | 742 | 0 |
| S59 | 12 | 1,2 | 1,8 | IRIIIIIIIIII | RRIIIIIIIIII | 4870 | 736 | 4683 |
| S60 | 12 | 1,2 | 8,5 | IIIIIIIIIIII | IIIIRIIIIIII | 5363 | 736 | 495 |
| S61 | 12 | 1,2 | - | IIIIIIIIIIII | RRIIIIIIIIII | 409 | 750 | 0 |
| S62 | 12 | 1,2 | 1,6 | IIIIIIIIIIII | IRIIIRIIIIII | 1084 | 736 | 414 |
| S63 | 12 | 1,2 | - | IIIIIIIIIIII | IIRIIIIIIIII | 1039 | 736 | 0 |
| S64 | 11 | 1,2 | - | IIIIIIIIIII | RIIIIIIIIRI | 5120 | 736 | 0 |
| S65 | 12 | 1,2 | - | IIIIIIIIIIII | RIIIIIIIIIII | 829 | 736 | 0 |
| S66 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIIII | 5084 | 738 | 0 |
| S67 | 12 | 1,2 | 6,1 | IIIIIIIIIIII | IIIIIIIIIIII | 1537 | 736 | 2833 |
| S68 | 12 | 1,2 | 1 | IIIIIIIIIIII | RIIIIIIIIIII | 387 | 736 | 223 |
| S69 | 12 | 1,2 | 5,3 | IIIIIIIIIIII | IIIIIIIIIIII | 4585 | 738 | 3505 |
| S70 | 12 | 1,2 | - | IIIIIIIIIIII | IRIIIIIIIIII | 2255 | 736 | 0 |
| S71 | 2 | 1,2 | 1,2 | UU | UU | 406 | 226 | 327 |
| S72 | 12 | 1,2 | 2 | IRIIIIIIIIII | RRIIRIIIIIII | 1778 | 736 | 793 |
| S73 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIRIII | 548 | 736 | 0 |
| S74 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIRII | 1257 | 736 | 0 |
| S75 | 12 | 1,2 | 1,6,7 | RIIIIIIIIIII | RIIIIIIIRIIR | 457 | 752 | 948 |
| S76 | 3 | 1,2 | 1 | UUU | III | 454 | 311 | 184 |
| S77 | 7 | 1,2 | 4,6,7 | IIIIIII | IIIRIRI | 431 | 653 | 533 |
| S78 | 12 | 1,2 | 1,3,8 | IIIIIIIIIIII | RIIIRIRIIIII | 807 | 740 | 884 |
| S79 | 11 | 1,2 | 6,1 | IIIIIIIIIII | RIIIIRIIIII | 631 | 738 | 829 |
