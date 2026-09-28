<!-- doc-head: governed recall journey and complete evidence delivery -->
<!-- Covers: paired fixture measurement, temporal admission, ledger latency and acceptance limits. -->
<!-- Authority: ROADMAP.md; this is implementation evidence, not a second backlog. -->
<!-- Runtime: installed and fresh-session delivery verified; semantic utility unmeasured. -->
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
  schema before timing the deliberately delayed transport. The
  fixture's 900 ms deadline, exact one-send assertion and fallback assertions remain.
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

At the initial source checkpoint, the recommendation was to integrate the
bounded renderer correction with its regression tests. That checkpoint was
source-tested only. The subsequent authorized installation and live delivery
checks are recorded below; they supersede its not-installed status. No disabled
feature was enabled and no mutating steward was run for verification. No
semantic quality watermark is earned from delivery alone.

Independent inspection also identified pre-existing limits outside this repair:
optional appended streams use a prompt filter without temporal-currentness
validation; lexical token fan-out can exhaust its pool before later tokens;
and hook text does not expose claim IDs/source citations to the consuming agent.
These findings prevent a claim of complete recall acceptance. They remain
recorded here under the existing ROADMAP task, without changing unrelated paths.

The original paired receipts bind the evaluator at commit `f2c0521`.
Reproduce with that revision's `python scripts/eval_recall_journey.py --json-out candidate.json`;
for the baseline run the same evaluator and fixture with `--source-root` pointing
to the preserved baseline checkout. Baseline reproduction requires matching
the renderer hash above. Outputs, temporary SQLite and sidecars are isolated;
production configuration must not be used as benchmark fixture state.

## Authorized installation and fresh-session acceptance

Following the operator's instruction to integrate, install and verify, `main`
fast-forwarded from `f5143d3` to `f2c0521`. Hashes of the three unrelated modified
files were preserved across integration. No unrelated work was staged or reset.

The wheel was built from the isolated checkout with no dependency resolution,
build isolation or network index. It retains package version **4.9.0**; identity
is the SHA256, not the unchanged version label:

- Installed wheel: `3db620800859ea0dfd7f63b888d2c2e1f4a63fce4a23609c0a5500d41bedf640`.
- Retained rollback wheel: `ad923b03621c00c5f78c3978cf5652d910b796c101adc1df21bad3199702a266`.
- Exactly two packaged files differ: `recall/context_hook.py` and
  `surfaces/cli_handlers_curation.py`. All **438** package files matched the
  retained baseline before installation and the candidate afterward in both the
  general and scheduled Python runtimes. No schema/configuration changes.
- Backup wheel and an executable, hash-checking `rollback.ps1` are retained at
  `artifacts/release-recall-20260927/`. Rollback reinstalls only the package in
  these two runtimes. No database restore is part of it.

On 2026-09-28 UTC (2026-09-27 locally), three fresh processes executed the actual
installed `UserPromptSubmit` recall hook against the authoritative database's
read-only recall path, with effective settings unchanged: JEV **live**, provider
deadline **1500 ms**, hook timeout **10 seconds**. Two prompts came from prior
operational questions; the third was an explicitly content-derived canary, not
an independent semantic-quality label.

| Installed observation | Result |
|---|---|
| Valid hook delivery, exit 0, within installed timeout | 3/3 |
| Whole hook wall time | 3867 / 5358 / 5700 ms |
| Complete claims exceeding 300 characters, also containing Unicode | 2 matched per delivery, 6 occurrences total |
| Decoded context size | 1789–2260 characters, below the default recall estimate budget |
| JEV provider outcome | 3/3 OK; served model `jev-1.13.0`; transport 500 / 547 / 516 ms |
| Hook stderr | Empty in all three runs |

An additional **real, new Claude Code session** used the installed recall hook
and effective provider settings, with tools disabled and only that hook enabled
in temporary test-process settings (no Stop/ingest hooks). Claude reproduced a
complete **1100-character** memory bullet that matched an existing public claim,
including Unicode and text beyond character 300. This verifies consumption by
the agent, beyond just a successful hook exit. Model reported: `claude-sonnet-5`.
The session used `--no-session-persistence`; no matching resumable transcript
file was found. The temporary settings file was removed afterward. Installed
settings were unchanged.

The exact session's hook log has `start` and successful `done`, with no error
event. Its additional JEV call also succeeded (`jev-1.13.0`, 484 ms). Across all
four calls the decision ledger reports **$0.000815976**. Claude reports
**$0.0213914 at list prices**; actual subscription/cash billing is unknown.
These measurements are not a general per-tier cost benchmark.

MCP remained available: `/healthz` and `/readyz` returned 200; unauthenticated
`/mcp` returned 401; authenticated initialization and listing returned 51 tools.
The initial `/health` and `/ready` probes were protected non-health paths (401),
not the documented health routes. No bulk restart was needed: changed hook/CLI
functions are loaded by fresh processes; existing MCP surfaces do not invoke
those modified functions. Existing stdio sessions were not interrupted.

Receipts: `deployment-summary.json`, `package-manifest.json`,
`live-hook-verification.json`, `fresh-claude-session.json`,
`installed-fixture-verification.json`, `mcp-health.json`, `log-verification.json`.

Independent installed-fixture verification passed **5/5 cases in each runtime**,
including all four positive expectations, scope/status/private/retired-support
exclusions, full Unicode tail and estimated-budget compliance, with zero provider
calls. The **14 renderer tests** also passed against the general installed package
from a temporary directory with `python -I` and no checkout on the import path.
No pytest dependency was installed into the scheduled runtime.

The first general-runtime evaluator attempt exposed a harness defect:
`--source-root` pointed at site-packages, moving a legacy `dataclasses` backport
ahead of Python's standard library. Normal installed imports and live hooks
were unaffected. The evaluator now has an explicit `--installed` mode that uses
an isolated child interpreter without changing import order; its regression
checks that boundary. The original failure and corrected outcomes remain in the
independent verification receipt. This is an evaluator-only repair, not another
installed-package change. Its focused test file passed **5 tests**; Ruff and
regenerated release-truth checks passed. GitNexus did not index this evaluator;
manual caller review confines it to the evaluation CLI and its tests.
Reproduce installed acceptance with
`python -I scripts/eval_recall_journey.py --installed --json-out installed.json`.

## Latency diagnosis and acceptance boundary

`latency-diagnosis.json` records disposable profiling of the baseline deadline
failures. SQLite SQL itself took under a millisecond; WAL commits and connection
close dominated. A warmed 1500 ms delayed-transport case took **1792.7 ms**:
send-intent commit/close took 134.5/131.8 ms, and final decision commit/close took
117.6/122.6 ms. It correctly returned a logged timeout and the legacy action.
The ledger's `engine_ms` was sampled before final persistence and understated
caller latency in that run. Cold schema preparation is an additional cost.

Thus **delivery acceptance passes for the installed checks above**, while the
strict deadline-plus-100-ms stress guarantee remains **FAIL**. This is an
observed baseline durability-path limit, not a renderer regression. Synchronous
durability, send-intent recording, installed deadlines and strict assertions
were not weakened to manufacture a pass. A connection-lifecycle change requires
its own measured core fix and regression gate; it is not silently included in
this two-file deployment. Semantic ranking quality on a fresh labeled cohort
remains **UNMEASURED**.

## Acceptance follow-up, 2026-09-28

The current activation review (`acceptance-20260928.json`) reads effective
settings and authoritative databases without running jobs or provider calls.
JEV is already live: 1,256 decision receipts in the preceding 24 hours, including
451 recall decisions, with 9 timeout fallbacks (0.72%). One additional orphan
send intent has no final receipt. Recorded token-rate cost is $0.097014918 plus
an orphan reserve estimate of $0.000169092; actual invoiced cost is unknown.
Seven-day activity is 45,007 receipts and 10 orphan intents. Lifecycle/detector
outcomes do not provide independently labeled semantic quality.

Skills selection has zero authorized skill claims and zero observed decisions;
it is unmeasured, despite the global live configuration. Filesystem skills are
not the governed SQLite skill catalog. Graph expansion is disabled with zero
confirmed entity-edge supports; its retained rows/jobs are not quality evidence.
Neither feature was activated or populated to manufacture acceptance.

A new graph-stream regression reproduced expired and future confirmed claims
re-entering prompt recall after ordinary retrieval had filtered them. The final
shared candidate boundary now applies the canonical temporal policy, including
malformed-bound rejection. The corrected seven-case regression passes; the
original source failed five cases and passed the two current-claim controls.
The first test draft also had incomplete fake-claim attributes; that harness
error was fixed before establishing the five-failure baseline.

The ignored main-checkout `memorymaster.egg-info` still advertised 4.8.9.
It was moved intact to the local acceptance artifacts as a rollback copy;
`importlib.metadata.version('memorymaster')` now resolves installed 4.9.0 from
that checkout. No source, package, configuration or database was modified by
that metadata cleanup.

### Connection lifetime and corrected timing interpretation

The final package retains the original ledger implementation. The following
experiment and its measurements explain why the proposed optimization was rejected.

An experimental bounded ledger scope reused one thread-local connection, retaining
separate committed transactions for the send intent and final receipt. Nested
scopes reuse it; a late-answer thread uses its own connection; the outer scope
closed synchronously. A close exception was contained instead of escaping the
engine's fallback handler. No PRAGMA values, deadlines or assertions were
relaxed. Six experimental regressions covered connection count, independently visible committed
intent, rollback recovery, thread isolation, handle cleanup and close errors.
The focused owner run passed 14 tests plus 2 public-demo tests; initial independent
review passed the combined 13 temporal/lifetime tests and Ruff. The experimental
wheel separately passed 27 temporal/lifetime/renderer tests in a disposable
environment and all five journey-fixture cases, with zero provider calls.

Two asynchronous close proposals were rejected before installation: one
accumulated threads/handles, and a bounded queue still caused checkpoint races
with subsequent decisions. Their failures remain in
`ledger-lifetime-20260928.json`.

**The connection-reuse experiment was also rejected before installation.** A
second review of the before-send durability boundary found that retaining the
connection can postpone the last-close checkpoint until after the provider call.
With WAL and NORMAL synchronization, that can weaken power-loss protection
relative to the old quiet, single-connection case. The original implementation
also has no unconditional power-loss guarantee when other connections remain
open. These limits follow SQLite's [synchronization contract](https://www.sqlite.org/pragma.html#pragma_synchronous)
and [WAL checkpoint behavior](https://www.sqlite.org/wal.html).
There was no demonstrated wall-time gain on the installed volume to justify
that change. The final package retains the original ledger implementation;
experimental code/tests and all measurements are preserved in local artifacts,
not installed. The engine change is documentation only, correcting its absolute
wall-clock claim. Its deadlines and executable behavior are unchanged.

**Storage was a confound in the earlier deadline diagnosis.** This shell inherits
TEMP/TMP on `T:`, whereas the installed decisions ledger is on `C:`. A disposable
three-trial WAL create/commit/close probe measured 640.26-690.89 ms on `T:` versus
31.14-39.54 ms on `C:`. The initial nine candidate stress trials on inherited
`T:` all exceeded deadline plus 100 ms; they remain failures for that environment.
Repeating the same seeded fake-transport trials explicitly on local `C:` gave:

| Deadline | Rejected experiment (ms) | Retained original ledger (ms) |
|---|---|---|
| 300 ms | 360.23 / 371.99 / 357.10 | 368.52 / 368.80 / 367.06 |
| 400 ms | 463.93 / 477.00 / 472.45 | 466.47 / 469.58 / 474.20 |
| 1500 ms | 1567.11 / 1565.11 / 1546.38 | 1562.31 / 1565.09 / 1566.08 |

Both experimental and original code pass all nine local-`C:` timing cases. Every case
records a timeout and exactly one fake send. This corrects the implication that
the inherited-temp failures demonstrated an installed-`C:` latency failure; it
does not establish a performance gain or a universal wall-clock guarantee.
The experiment reduced connection opens/closes but was not selected. Synchronous
filesystem work can still exceed the transport/lock-wait budget on a slow disk.

The general gate was restarted against the final frozen source and explicit
local-`C:` fixture paths. Partial preliminary logs are retained under the local
acceptance artifacts: one run preceded the final close-error regression, and
another used inherited `T:`, and a third tested the subsequently rejected ledger
experiment. None of those runs counts as the final selected-package gate.

The first selected-package shard reported 1305 passed and two failures in the
real skills-adapter integration test. The gate wrapper had forced the global
JEV mode to off, overriding the test's legacy enable flag and preventing its
fake transport calls. Correcting the wrapper to remove inherited mode/keys and
use the product's default-off behavior restored the intended test configuration:
the complete skill-catalog file passed all 19 tests without source/test edits.
The original two failures are retained. The full suite was restarted with the
corrected isolated environment; this is harness correction, not selector repair.

The corrected gate's second shard exposed four failures in two older mock
fixtures: their `MagicMock` claims did not define `valid_from`/`valid_until`,
so the strict temporal parser correctly rejected the mock-valued attributes.
Both fixture helpers now explicitly use `None` for unbounded validity, as the
real claim model does. Assertions and production guards are unchanged.
All 33 tests in the two complete affected files plus the new temporal regression
pass. Independent diff review confirms this is fixture-only repair; the original
four-failure log is retained and the complete affected shard will be repeated.

The fourth shard revealed the same omission in the BM25 helper (four failures)
and three direct hook fixtures (three failures). Isolated runs reproduced both
sets. These mocks now also declare unbounded validity explicitly; no assertions
were edited. The combined four affected files and temporal regressions pass
all 61 tests. Both affected full shards are repeated for final acceptance.

### Final selected-source gate

The full non-ML gate is complete: **6,707 passed, 0 failed, 75 skipped,
90 deselected, 1 expected failure** across 534 test files. Shards 1/3 passed
unchanged; complete shards 2/4 passed after the fixture-only repairs. Executable
source was identical throughout, and source/tests remained byte-identical during
the replacement runs. The original 11 failures and all replacement logs are
retained; `full-gate-20260928.json` records the counts and log hashes. Ruff,
release-truth verification and diff-check also pass. Independent review accepted
the temporal guard and confirmed all six repaired mock sites changed only their
optional validity attributes.

Selected wheel SHA-256:
`42d64016b07debb32ada961c368cca4af8f7350a944b470bb1283a31736c166b`.
Its disposable installed-package run passed 21 temporal/renderer tests and all
five journey cases with network egress blocked and zero provider calls. Only the
temporal admission predicate changes executable behavior; engine documentation
and one newline-only packaged file account for the other manifest differences.
