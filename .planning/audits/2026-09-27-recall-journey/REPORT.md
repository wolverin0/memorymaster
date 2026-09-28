<!-- doc-head: governed recall journey and complete evidence delivery -->
<!-- Covers: paired fixture measurement, renderer repair and acceptance limits. -->
<!-- Authority: ROADMAP.md; this is implementation evidence, not a second backlog. -->
<!-- Production: no installation, settings change, provider call or DB mutation. -->
<!-- /doc-head -->
# Recall journey review — 2026-09-27

The observed defect is loss of evidence between selection and delivery: the
prompt renderer truncated every claim at 300 characters, replaced Unicode with
question marks, stopped packing at the first oversized row, and omitted framing
from its token estimate. The repair preserves complete text and skips claims
that cannot fit. It does not improve semantic ranking by itself.

Implementation branch: `fix/recall-journey-20260927`, based on
`f5143d3ef64ac6ce84c2b0198940a14e9ecbfb90`. The original checkout's unrelated work
was preserved. SQLite remains the authority.

## Observed installed baseline

The installed distribution is 4.9.0. The installed recall hook source matches
the baseline source byte for byte (SHA256
`8e5e0a82fd4cdf6959d61f825b43433c90aeefce9b2ef3c311f340ec628ef7ad`).
Installed Claude settings and the inherited environment declare JEV **live**.
The evaluator explicitly overrides its surfaces to **off**; scripted unit tests
exercise JEV without contacting TypeSafe. These are separate modes, not evidence
that installed JEV was disabled or re-evaluated.

The authoritative database was opened with SQLite `mode=ro` and
`PRAGMA query_only=ON`. Among confirmed, public claims in the project scope,
527 rows were counted, 128 exceeded 300 characters and 99 contained non-ASCII
characters. These aggregates establish exposure to truncation/encoding loss;
they do not establish current temporal/support eligibility or usefulness.
See `installed-baseline.json`; no claim text is copied from production.

## Paired measurement

The evaluator uses the real service, disposable SQLite, explicit lifecycle
promotion, the public source-retirement operation, prompt recall and decoded
hook delivery. Five frozen queries include four declared positive expectations
and one exclusion-only case. Scope, stale, private and retired-support claims
are negative controls. A long claim carries its distinguishing Unicode tail
beyond character 300; an oversized claim tests complete-bullet packing.

`baseline.json` and `candidate.json` are the durable paired receipts. They must
have identical fixture and evaluator hashes. Each records the imported renderer
hash, source version, distribution label and source Git HEAD. The original
checkout has stale 4.8.9 distribution metadata; its source pyproject is 4.9.0
and its renderer matches the installed 4.9.0 baseline. The imported source hash,
not that stale metadata label, identifies the implementation measured.

| Check | Baseline | Candidate | Interpretation |
|---|---:|---:|---|
| Queries completed | 5/5 | 5/5 | Disposable fixture, not production traffic |
| Positive candidate coverage | 4/4 | 4/4 | All declared positives reached the observed candidate stage |
| Ranked hit@5 | 4/4 | 4/4 | Trace is input to renderer after selection, not an independent ranking implementation |
| Rendered ID hit@5 | 4/4 | 4/4 | IDs alone concealed the truncated-answer defect |
| Fixed-denominator precision@5 | 0.20 | 0.20 | One declared positive per positive query; divide by five even for shorter outputs |
| Full expected text in decoded delivery | 3/4 | 4/4 | Concrete improvement: complete long Unicode claim reaches the hook envelope |
| Forbidden claims excluded | 5/5 queries | 5/5 queries | Four forbidden claim IDs checked per query |
| Exact SQLite citation source strings | 9/9 rendered occurrences | 8/8 rendered occurrences | Not delivered citation/support correctness |
| Remote/provider calls | 0 | 0 | Egress blocked; no paid call |
| Observed latency P50 / P95 | 29.931 / 116.250 ms | 16.006 / 77.337 ms | Five samples each; descriptive only |
| Estimated recall budget respected | 5/5 | 5/5 | 640–800 characters by case; narrow-budget regression cases are separate |

Latency and per-case budgets are recorded in the receipts. P95 uses nearest
rank (for five samples it is the maximum). A single run per implementation with
five cases cannot establish a speed improvement. Token budgets use the existing
four-characters-per-token estimate, not a tokenizer guarantee. Delivery framing
outside the recall markdown is reported separately from the renderer budget.

Quality on current real prompts, installed provider latency/cost and downstream
agent task success remain **UNMEASURED**. The historical 953-prompt labels are
not reused as current truth: their lifecycle eligibility changed. This fixture
does not replace a fresh independently labeled semantic-recall benchmark.

## Repair and verification

- `memorymaster/recall/context_hook.py`: complete claim text, Unicode preserved,
  skip non-fitting rows, count header/separators/flags in estimated budget.
- JEV excludes individually non-fitting candidates before its top-20 cap and
  uses conservative flagged output costs. Ranking input still uses a
  300-character prefix; this repair makes no claim about its semantic efficacy.
- `memorymaster/surfaces/cli_handlers_curation.py`: legacy console encodings
  escape unsupported characters at the output boundary. UTF-8 delivery retains
  Unicode. The renderer itself no longer destroys it.
- `tests/test_recall_rendering_integrity.py`: 14 passing focused cases for full
  tails, Unicode, small budgets, starvation, JEV capacity and UTF-8/cp1252.
  The original renderer produced eight failures in the initial 12-case red run;
  the subsequent two CLI cases separately reproduced the console failure.
- First affected-suite run: **179 passed, 1 failed**. The failed hook deadline
  test expected one send but observed zero. Loading the original four renderer
  functions into the same worktree reproduced the failure; the ledger recorded
  pre-send timeout. This is not renderer causality.
- Bounded test setup repair: pre-register the disposable ledger's question
  schema before timing the deliberately delayed transport. The production
  900 ms deadline, exact one-send assertion and fallback assertions remain.
  The repaired test passed. No production engine changes were made.
- Three supplemental direct-engine deadline tests failed elapsed-time limits
  both here and in the original checkout. Legacy/timeout assertions passed.
  These baseline failures remain visible; this review does not claim a globally
  green suite or a production latency acceptance.
- Independent review passed renderer, CLI, JEV capacity and the test-only
  deadline setup repair. It explicitly limited citation evidence to SQLite
  source strings and the ranked trace to renderer input.
- Final evaluator regressions: **4 passed**. Failed positive cases retain their
  IDs in the denominator and score zero; absent positives remain unmeasured.
  Exact source mismatch, P95 and per-case budget bounds are tested. Final paired
  receipts use the same evaluator/fixture hashes, verified independently.
- Fresh final public lifecycle checks: **13 passed** in 158.29 seconds. Ruff,
  release-truth generation/check and diff checks passed. GitNexus impact and
  change detection reported LOW; raw diff review resolved overbroad automatic
  file/symbol mapping. `verification.json` records counts; original failure
  traces are retained alongside it with personal paths removed.

## Remaining limits and release recommendation

Recommend integrating the bounded renderer correction with its regression
tests through the normal release path. It repairs demonstrated evidence loss;
it is **source-tested, not installed or live-accepted**. This task did not enable
any disabled feature, change production configuration or run a mutating steward.
No semantic quality watermark is earned from delivery alone.

Independent inspection also identified pre-existing limits outside this repair:
optional appended streams use a prompt filter without temporal-currentness
validation; lexical token fan-out can exhaust its pool before later tokens;
and hook text does not expose claim IDs/source citations to the consuming agent.
These findings prevent a claim of complete recall acceptance. They remain
recorded here under the existing ROADMAP task, without changing unrelated paths.

Reproduce with `python scripts/eval_recall_journey.py --json-out candidate.json`;
for the baseline run the same evaluator and fixture with `--source-root` pointing
to the preserved baseline checkout. Baseline reproduction requires matching
the renderer hash above. Outputs, temporary SQLite and sidecars are isolated;
production configuration must not be used as benchmark fixture state.
