<!-- doc-head: T-1433 AC3 two-judge replay of index + on-demand reading versus similarity injection -->
<!-- Covers: method, snapshot/prompt provenance, both judges, cost/noise/recall per strategy, intervals, recommendation. -->
<!-- Evidence: 80 real prompts 2026-10-23..30, read-only DB snapshot, Gemini 4 Argon + Claude Sonnet blind judges. -->
<!-- Supersedes the single-judge AC3 section of AC3-comparison.md (merged in PR #273). -->
<!-- /doc-head -->
# T-1433 AC3: index + on-demand reading vs similarity injection (two blind judges)

Measured 2026-10-30 at source `73e71c1` with `scripts/eval_index_vs_similarity.py`.
It replaces the AC3 comparison in `AC3-comparison.md` (PR #273, merged), which used
one GPT judge and held the candidate pool to the two injected claims.

**Result.** Over 79 real prompts, index + on-demand reading (B) cost **1,470
characters per prompt against 2,154** for similarity injection (A, −32%). It put
**0.52 irrelevant bodies per prompt into context against 1.30** (−60%, consensus
labels). Relevant recall was **54% against 33%** under consensus and Gemini labels,
and **tied at 33%** under Claude labels. All three cost and noise intervals exclude
zero. The recall gain excludes zero under consensus and Gemini labels, but not
under Claude labels.

**Recommendation: build B as an opt-in pilot and keep A as the default.** The pilot
injects a top-8 index and exposes a read-by-id tool. Change the default only after a
live pilot shows that real agents call the read tool about as often as the
instructed reader here (45 of 79 prompts), and that the read round-trip's latency
is acceptable. The larger problem is upstream: only 46 of 864 pool candidates (5.3%)
are consensus-relevant, and 93% of index titles are irrelevant. Retrieval
precision, through the threshold and claim titles, matters more than the delivery
format.

## Method

- **Snapshot.** The claims DB was copied with the sqlite3 backup API from a
  `mode=ro` connection, about 22:20 UTC on 2026-10-30. Everything was read from the
  copy, opened `read_only=True`. `_finish_recall` was never called, so no access
  record was spooled, no session state was written and no telemetry line was
  appended. Candidate IDs came from the running dense service (EmbeddingGemma 2,
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
- **Privacy.** Home paths and private IPv4 addresses were masked in everything sent
  to a model. Prompt and claim text stayed in the session scratchpad. This file
  publishes only ordinals, positions, labels and counts.
- **Transport notes.** OpenCode's OpenAI route returned 401, its Google route had
  no API quota, and DeepSeek hit its weekly cap. A GPT-family judge was therefore
  not available.

## Results (79 events, 864 candidates)

| Measure | Consensus | Gemini 4 Argon | Claude Sonnet |
|---|---:|---:|---:|
| Pool candidates R / I / other | 46 / 695 / 123 | 47 / 775 / 42 | 129 / 723 / 12 |
| A chars per prompt | 2,154.47 | same | same |
| B chars per prompt (index + reads) | 1,469.77 (704.03 + 765.75) | same | same |
| A irrelevant bodies / judged bodies | 103/118 = **87.3%** | 130/146 = 89.0% | 113/156 = 72.4% |
| B irrelevant reads / judged reads | 41/66 = **62.1%** | 57/83 = 68.7% | 43/86 = 50.0% |
| B irrelevant index titles / judged titles | 465/502 = 92.6% | 531/569 = 93.3% | 488/593 = 82.3% |
| Irrelevant bodies per prompt, A to B | **1.304 to 0.519** | 1.646 to 0.722 | 1.430 to 0.544 |
| Relevant recall vs pool, A | 15/46 = **32.6%** | 16/47 = 34.0% | 43/129 = 33.3% |
| Relevant recall vs pool, B | 25/46 = **54.3%** | 26/47 = 55.3% | 43/129 = 33.3% |
| Relevant claims reachable in B's index | 37/46 | 38/47 | 105/129 |
| Prompts with a relevant body / prompts with one in pool, A | 13/22 = 59.1% | 14/23 = 60.9% | 33/48 = 68.8% |
| Same, B | 17/22 = 77.3% | 18/23 = 78.3% | 27/48 = 56.3% |

Totals: A 170,203 characters with 158 bodies. B used 55,618 index plus 60,494 read
characters, with 88 bodies read on 45 of 79 prompts.

Paired bootstrap of B − A (2,000 resamples of prompts, seeded, 95% percentile):

| Label set | Chars per prompt | Irrelevant bodies per prompt | Relevant recall |
|---|---|---|---|
| Consensus | [−1,099.5, −285.9] | [−0.987, −0.582] | [+0.037, +0.367] |
| Gemini | [−1,103.0, −264.6] | [−1.139, −0.709] | [+0.035, +0.359] |
| Claude | [−1,096.0, −280.2] | [−1.089, −0.684] | [−0.109, +0.107] |

Judge agreement on R versus not-R over 864 candidates: raw 90.3%, Cohen's kappa
**0.48** (moderate). Claude Sonnet marks about 2.7 times as many candidates R, and
most disagreements are Claude-R / Gemini-I. Recall therefore depends on the judge,
while cost and noise favour B under every label set.

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
  is the one under which B shows no recall gain. Gemini labels give the
  family-independent view.
- **Pool-relative recall.** "Relevant recall" counts only the authorized pool of up
  to 12 candidates. Relevant claims below the threshold or outside the dense index
  are not counted.
- **Short-ID collisions.** Short IDs are not unique across scopes. Five pools held
  two to four claims with the same short ID: one ID was shared by six claims in
  four scopes, admitted through the outside-scope path. The evaluator renames them
  `<id>~<claim id>` so that reads resolve. A production read-by-id tool must
  resolve the same ambiguity.

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
| S01 | 12 | 1,2 | 2,3 | IIIIIIIIIIII | IIIIIIIIIIII | 3832 | 736 | 311 |
| S02 | 12 | 1,2 | 4,6 | IIIIIIIIIIII | IIIIIIIIIIII | 3843 | 740 | 2134 |
| S03 | 12 | 1,2 | 1,3 | IIIIIIIIIIII | IIIRRRRRIIRI | 412 | 736 | 250 |
| S04 | 12 | 1,2 | 1,6 | RIIIIIIIIIII | RIIIIIIIIIRI | 457 | 752 | 397 |
| S05 | 12 | 1,2 | 1 | IIIIIIIIIIII | IIIIIIIIIIII | 3372 | 746 | 3129 |
| S06 | 12 | 1,2 | 1,2 | IIIIIIIIIIII | RIIIIIIIIRIR | 390 | 736 | 311 |
| S07 | 12 | 1,2 | 2,1 | IRIIIIIIIIII | IRIIIIRRIIII | 458 | 759 | 379 |
| S08 | 11 | 1,2 | - | IIIIIIIIIII | IIIIIIIIIII | 4946 | 740 | 0 |
| S09 | 12 | 1,2 | 1,8 | IIIIIIIIIIII | RIIIIIIIIIII | 1495 | 738 | 820 |
| S10 | 7 | 1,2 | 1,2 | RIIIIII | IRIIIII | 4771 | 653 | 4692 |
| S11 | 12 | 1,2 | 3,4,6 | IIRRRRIIIIRI | IIRRRRIIIIRI | 1687 | 740 | 5847 |
| S12 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIIII | 6467 | 738 | 0 |
| S13 | 12 | 1,2 | 1 | RIIIIIIIIIIR | RIIIIIIIIIIR | 1977 | 736 | 159 |
| S14 | 12 | 1,2 | 4,5,7 | IIIIIIRRRIII | IIIIIIRRRIII | 441 | 738 | 810 |
| S15 | 5 | 1,2 | 2,4 | IIIRI | IRIRI | 3816 | 486 | 594 |
| S16 | 2 | 1,2 | - | II | II | 4657 | 226 | 0 |
| S17 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIIII | 5100 | 736 | 0 |
| S18 | 12 | 1,2 | 1 | IIIIIIIIIIII | IIIIIIIIIIII | 1748 | 742 | 145 |
| S19 | 12 | 1,2 | 3,5 | IIIIIIIIIIII | IIIIIIIIIIII | 322 | 736 | 348 |
| S20 | 12 | 1,2 | - | IIIIIIIIIIII | RIIIIIIIIIII | 369 | 738 | 0 |
| S21 | 12 | 1,2 | 2,8 | IIIIIIIIIIII | IIIIIIIIIIII | 4509 | 736 | 1609 |
| S22 | 12 | 1,2 | 4,3 | UUUUUUUUUUUU | UUUUUUUUUUUU | 249 | 685 | 294 |
| S23 | 12 | 1,2 | - | IIIIIIIIIIII | RRRRRRIRIIII | 1316 | 736 | 0 |
| S24 | 10 | 1,2 | 2,5 | IRIRIIIIRI | IRIRRIIIRR | 3900 | 736 | 1864 |
| S25 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIIII | 3786 | 740 | 0 |
| S26 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIIII | 3420 | 740 | 0 |
| S27 | 12 | 1,2 | 1,2 | RIIIIIIIRIII | RIRIIRIIRIII | 458 | 750 | 379 |
| S28 | 9 | 1,2 | - | IIIIIIIII | IIIIIIIII | 515 | 736 | 0 |
| S29 | 12 | 1,2 | 1,6,2 | RRIIIRIRIIIR | RRIIIRRRIIIR | 448 | 736 | 1393 |
| S30 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIRIIII | 2300 | 738 | 0 |
| S31 | 3 | 1,2 | 1 | UUU | III | 1124 | 313 | 931 |
| S32 | 12 | 1,2 | 7 | IIIIIIRIIIII | IIIIIIRIIIII | 5540 | 736 | 1838 |
| S33 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIRII | 2500 | 736 | 0 |
| S34 | 4 | 1,2 | - | IIII | RIRI | 439 | 396 | 0 |
| S35 | 12 | 1,2 | 4,5 | IIIRIIIIIIII | IIIRIIIIIIII | 5036 | 736 | 1747 |
| S36 | 12 | 1,2 | 5,2 | UUUUUUUUUUUU | IRIIRIIIIIIR | 400 | 736 | 325 |
| S37 | 12 | 1,2 | - | IIIIIIIIIIII | RIIIIIIIIIII | 1545 | 736 | 0 |
| S38 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIIII | 2924 | 736 | 0 |
| S39 | 12 | 1,2 | 1,2,4 | RRRRIIIIIIRI | RRRRIIIIIIRI | 2043 | 738 | 2603 |
| S40 | 8 | 1,2 | - | IIIIIIII | IIIIIIII | 447 | 713 | 0 |
| S41 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIIII | 3153 | 736 | 0 |
| S42 | 9 | 1,2 | - | RIIIIIIII | RIIIIIIII | 2280 | 736 | 0 |
| S43 | 12 | 1,2 | 7,5 | IIIIIIIIIIII | RRIIIIRIIIII | 424 | 736 | 341 |
| S44 | 9 | 1,2 | 3 | IIIIIIIII | RRIIIRRII | 4041 | 736 | 1093 |
| S45 | 12 | 1,2 | 1,8,4 | RIIRRIIRRIII | RIIRRIIRRIII | 366 | 736 | 507 |
| S46 | 12 | 1,2 | 7 | IIIIIIIIIIII | IIIIIIIIIIII | 3847 | 736 | 753 |
| S47 | 12 | 1,2 | - | IIIIIIRIIIII | RRRIRIRRRRII | 393 | 736 | 0 |
| S48 | 12 | 1,2 | - | IIIIIIIIIIII | RIIIIIIIIIII | 2628 | 748 | 0 |
| S49 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIIII | 427 | 738 | 0 |
| S50 | 12 | 1,2 | 4,6 | IIIIIIIIIIII | IIRRIIRIIIII | 4946 | 736 | 667 |
| S51 | 12 | 1,2 | 3,4,1 | IIRIIIIIIIII | RRRIIRIRIIII | 2039 | 736 | 2491 |
| S52 | 12 | 1,2 | 8,7 | IIIIIIIIIIIR | IIIIIIIIIIIR | 4436 | 738 | 3397 |
| S53 | 12 | 1,2 | 3,8 | RIRIIIIIIIII | RIRIIIIIIIII | 1446 | 729 | 876 |
| S54 | 12 | 1,2 | 2,4 | IIIRIIRIIIII | IIIRIIRIIIII | 451 | 736 | 593 |
| S55 | 11 | 1,2 | - | IIIIIIIIIII | IIRRIIIIIII | 4320 | 740 | 0 |
| S56 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIIII | 295 | 740 | 0 |
| S57 | 10 | 1,2 | - | UUUUUUUUUU | IIIIIIIIII | 274 | 744 | 0 |
| S58 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIRIII | 1647 | 742 | 0 |
| S59 | 12 | 1,2 | 1,2 | IRIIIIIIIIII | RRIIIIIIIIII | 4870 | 736 | 4791 |
| S60 | 12 | 1,2 | 5,8 | IIIIIIIIIIII | IIIIIIIIIIII | 5363 | 736 | 495 |
| S61 | 12 | 1,2 | - | IIIIIIIIIIII | RRIIIIIIIIII | 409 | 750 | 0 |
| S62 | 12 | 1,2 | 2 | IIIIIIIIIIII | IRIIIIIIIIII | 1084 | 736 | 757 |
| S63 | 12 | 1,2 | - | IIIIIIIIIIII | RIRIRIIIIIII | 1039 | 736 | 0 |
| S64 | 11 | 1,2 | - | IIIIIIIIIII | IIIIIIIIIII | 5120 | 736 | 0 |
| S65 | 12 | 1,2 | - | IIIIIIIIIIII | RIIIIIIIIIII | 829 | 736 | 0 |
| S66 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIIII | 5084 | 738 | 0 |
| S67 | 12 | 1,2 | 6,7 | IIIIIIIIIIII | IIIIIIIIIIII | 1537 | 736 | 2782 |
| S68 | 12 | 1,2 | 1,6 | IIIIIIIIIIII | RIIIIIIIIIII | 387 | 736 | 1765 |
| S69 | 12 | 1,2 | 5 | IIIIIIIIIIII | IIIIIIIIIIII | 4585 | 738 | 1801 |
| S70 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIIII | 2255 | 736 | 0 |
| S71 | 2 | 1,2 | - | UU | II | 406 | 226 | 0 |
| S72 | 12 | 1,2 | 6,1 | IRIIIIIIIIII | RRIIRRIIIIRI | 1778 | 736 | 1618 |
| S73 | 12 | 1,2 | 2,4,5 | IIIIIIIIIIII | IIIIIIIIRIII | 548 | 736 | 519 |
| S74 | 12 | 1,2 | - | IIIIIIIIIIII | IIIIIIIIIRII | 1257 | 736 | 0 |
| S75 | 12 | 1,2 | - | RIIIIIIIIIII | RIIIIIIIRIIR | 457 | 752 | 0 |
| S76 | 3 | 1,2 | - | UUU | III | 454 | 311 | 0 |
| S77 | 7 | 1,2 | 4,7 | IIIIIII | IIIIIII | 431 | 653 | 353 |
| S78 | 12 | 1,2 | 5,7,1 | IIIIIIIIIIII | RIIRRIRIIIII | 807 | 740 | 874 |
| S79 | 11 | 1,2 | 3 | IIIIIIIIIII | IIIIIRIIIII | 631 | 738 | 712 |
