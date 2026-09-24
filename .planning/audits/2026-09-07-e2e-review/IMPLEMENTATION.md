<!-- doc-head: source candidate delivered and selection v2 installed; current live evidence linked below -->
Covers: seven remediation packages, verification, candidate packaging and rollback.
Baseline: b0dd976; original REPORT.md and its red evidence remain historical.
Read when continuing delivery; ROADMAP.md remains the sole roadmap.
Acceptance: source/model checks and current installed evidence remain distinct from historical human labels.
<!-- /doc-head -->

# Implementation checklist

- [x] Isolated worktree; original checkout preserved; four red contracts reproduced.
- [x] E2E-01: additive ledger migration, opt-in resume, historical inventory, replay regressions.
- [x] E2E-02: exact serialized context budget and matching receipts in all formats.
- [x] E2E-03: atomic attempt supervision, phase timing, freshness, 24/25 minute policy.
- [x] E2E-04: exact Origin/Referer/Host checks including legacy and ephemeral binds.
- [x] E2E-05: frozen stratified cohort and AI preparation using existing evaluation.
- [x] E2E-06: decision summary, review/search priority, scoped diagnostics, browser desktop/mobile.
- [x] E2E-07: opt-in core MCP profile, Hermes fixture journey, discovery reduction >=50%.
- [x] Focused tests, full non-ML suite, Ruff, release metadata and candidate wheel/hash.
- [x] Installation/rollback procedure, package commits and final evidence.

## Delivery boundaries

The original candidate was delivered without installation. The operator then
requested operational completion: [LIVE-DEPLOYMENT.md](LIVE-DEPLOYMENT.md) records
the installation, service restart and actual runtime evidence. Retain the additive
ledger migration when reverting any code package. Historical recovery and
production restore were not performed.

The old cohort's human-provenance metric (50 labeled decisions, including 20 human
reviews) remains unsatisfied and its thresholds are unchanged. It is a preserved
historical evaluation, not an outstanding technical QA assignment to the operator.
Bounded provider evidence is not a production precision estimate.

## Evidence

Baseline normal-suite replay: `tests/test_e2e_review_contracts.py`: four failures,
matching REPORT.md (resume, serialized JSON budget, selected rows, lookalike Origin).

Source verification so far:

- E2E-01 `e785071`: 25 focused tests passed; old SQLite fixture migrates additively and retains extraction rows.
- E2E-02 `f00cc25`: 95 existing/contract tests passed (Origin remained red at that stage); 20 new delivery tests passed.
- E2E-03 `4828e77`: 27 focused tests passed plus the no-pane/prior-receipt regression; actual supervised fixture child ran all seven phases.
- Ruff passed for these changed Python modules; final broad verification is recorded below.

- E2E-04 `998ba9b`: 49 auth/Origin tests passed; all four original red contracts now pass.
- E2E-05: 24 sampler/evaluator/cohort tests passed. Frozen cohort v1: population 73,
  selected 46 captures (20 zero-candidate, 6 candidates/no-action, 20 with actions),
  50 candidates plus 20 negative controls = 70 AI-labeled decisions. Raw redacted
  review inputs remain local in `artifacts/e2e-fixes-20260907/cohort-v1`.
  AI-only useful precision 0.7692, recall 0.3226; human reviews 0/20. Only 13 emitted
  decisions exist in this cohort: a later, non-overlapping cohort is required to
  reach the human minimum. Acceptance PENDING; no model/prompt/threshold changes.
  Evidence-exact, semantic sufficiency, validity, scope and usefulness are separate.
  Usage covers the whole time interval, without invented savings attribution.

- E2E-06: 19 focused dashboard tests passed, including authorized scope counts,
  empty/error/stale distinction and real HTTP candidate -> confirmed with citation
  -> source retirement -> excluded. Found and fixed the empty-query fallback
  that dropped scope; trusted Search now excludes stale/conflicted by default.
  Chrome fixture journey passed with visible citation and retirement preview/apply.
  Empty, pending, healthy, stale and read-error summaries inspected at desktop and
  mobile breakpoint (requested 390px viewport; CSS width 355px at browser zoom).
  No document horizontal overflow after constrained grid columns; tables scroll
  inside their panels. Only an unrelated browser-extension console error remained.
  Dashboard server and viewport override were task-local; no installed services changed.

- E2E-07: opt-in discovery profile; full remains default. Canonical tools/list result
  fell from 51 tools / 48,396 bytes to 4 tools / 3,889 bytes (91.9642%). Actual
  authenticated HTTP Hermes fixture passed under both profiles: delivery, scope
  bind/show/clear, preview retirement, cited skill recall, improve, wrong-token
  denial and writer delete denial. Core changes advertisement only: named legacy
  calls remain available under unchanged authorization for adapter compatibility.
  Existing installed clients were not modified. Two profile tests and Ruff pass.

## Frozen usefulness evidence

Cohort v1 interval: `[2026-09-05T15:38:34.710902Z, 2026-09-08T01:50:26.990892Z)`.
Sources SHA-256: `ca01d3bc5822e0c08f1504a6283c6f6ae393dc1c2a5ddb63e3a921d881ac575d`.
AI-only metrics: exact evidence 1.0, ephemeral rejection 0.9231, scope 0.5385,
action accuracy 0.6923, useful precision 0.7692, useful recall 0.3226.
These provisional judgments are not human-validated effectiveness measurements.

| Provider/model | Calls | Input tokens | Output tokens | Summed latency ms | Errors |
|---|---:|---:|---:|---:|---:|
| antigravity / gemini-3.7-flash-low | 21 | 518,563 | 13,953 | 249,962 | 2 |
| google / gemini-3.5-flash-lite | 68 | 551,582 | 13,006 | 1,139,863 | 1 |

Usage covers all calls in the interval, not just sampled or useful decisions.
Provider labels are recorded ledger values; no model was selected or changed here.
No savings or cost-per-useful-memory figure was inferred.

## Candidate and validation corrections

- The first broad run exposed an accidental SQL-placeholder edit from display
  cleanup; restored in `b03fddf`, with all 9 attribution contracts passing.
- The facade size gate rejected 11 excess dashboard lines. `da1c505` moves the
  unchanged HTML literal into `dashboard_template.py`; byte-identical literal
  comparison and 30 architecture/UI/release/scope tests passed. The size gate was
  not relaxed.
- A diagnostic four-worker run finished with 16 failures, 5,030 passes,
  77 skips and one expected failure. The existing autouse cleanup prunes a shared
  test directory after each test; concurrent workers deleted each other's files.
  This run is not a passing gate. The final full gate runs sequentially, without
  concurrent pytest processes. Temporary xdist tools stayed in ignored artifacts;
  no dependency or shared cleanup changes were introduced.
- `92ec81e` updates the existing version-override wrapper regression for the new
  supervisor path; all eight version tests pass, including actual PowerShell
  argument forwarding. The package wheel is unaffected by this test-only change.
- Wheel smoke imported the extracted wheel, not the source checkout. Public
  lifecycle/citations/source retirement/observations/core discovery passed.

## Original candidate identity - September 7

Unpublished wheel: `memorymaster-4.8.9-py3-none-any.whl`.
Packaged code commit: `da1c5052326257aa643cadaea919d8888029ac43`.
Wheel SHA-256: `aadbb2abaa960f1d0c2871748d1c42ca2d7cc4334eacec0edac90657d210e6a6`.
The local release manifest binds every packaged source file to its SHA-256;
the final source archive additionally carries the installer, tests and procedure.
The version was not promoted and no artifact was installed or published.
See [operator procedure](../../../docs/e2e-candidate-deployment.md).

## Final technical gate

Sequential `python -m pytest tests/ -m "not ml" -v --tb=short` on `92ec81e`:
**5,048 passed, 75 skipped, 90 deselected, 1 xfailed, 17 warnings** in 1,133.41s;
process exit 0. XML and complete log are retained at
`artifacts/e2e-fixes-20260907/non-ml-sequential.{xml,log}`. Skipped tests and the
expected failure are not claimed as verified outcomes; ML behavior was unchanged.
Ruff passed for every changed Python file. Release-truth `--check` and
`git diff --check` passed. Browser and extracted-wheel evidence are above.

Source verification and candidate packaging are complete. Human quality remains
PENDING (0/20 reviews; only 13 emissions available); installation, live scheduler
proof and historical recovery were not performed. The original checkout and
unrelated untracked directories were preserved.

## Resumption plan - 2026-09-08

The operator requested explicit model/effort allocation and parallel work without
inheriting the coordinator's maximum reasoning configuration. Candidate `2409c5e`
and its existing verification remain the baseline; reviews do not invalidate or
replace that evidence. The existing two agents are reused for any follow-up work.

| Work | Model / effort | Why this level | Dependency / completion |
|---|---|---|---|
| Delivery artifacts and human-review workflow | Luna / high | Bounded file and evidence reconciliation | Parallel read-only review; return concrete remaining tasks |
| Dreaming resume, recall receipts, Origin and scope boundaries | Terra / high | Multi-step lifecycle and authorization reasoning | Parallel read-only review; findings need a trigger and consequence |
| Bounded delivery or evaluation fixes, if demonstrated | Existing Luna / high | Isolated implementation with explicit ownership | After triage; focused outcome tests |
| Lifecycle or authorization fixes, if demonstrated | Existing Terra / high | Correctness across state transitions and callers | After review; failing regression, fix, focused verification |
| Integration, acceptance and scope decisions | Coordinator | Resolve cross-task dependencies without duplicating review | Consume concise evidence; escalate only a concrete unresolved issue |

Execution constraints: two child agents, explicit model overrides, short task
briefs, no inherited full conversation, no nested agents, no Ultra/fast workers.
Each implementation task receives file ownership and a completion criterion.
Pytest execution is serialized because the existing cleanup shares temporary
paths. Re-run a broad gate only for changes that justify it. At that source
checkpoint, actual human labels, installation, restarts and historical recovery
were separate decisions. Subsequent authorized installation and restarts are
recorded in [LIVE-DEPLOYMENT.md](LIVE-DEPLOYMENT.md).

- [x] Verify current candidate/worktree and previous delivery boundaries.
- [x] Reconcile both bounded reviews into concrete follow-up tasks.
- [x] Fix and verify substantiated defects using the allocated workers.
- [x] Refresh changed delivery evidence; report remaining human/operator gates.

### Resumption outcome

Luna verified the original candidate hashes and the immutable cohort. Its review
found that human overrides with empty rationale could count toward acceptance,
contrary to the documented review contract. Luna fixed the guard and added tests;
Terra independently approved that diff. Terra's separate Dreaming/recall/Origin/
scope review found no substantiated defect within that scope, not a general
security certification. Two agents were used; no additional agents were spawned.

Code commit `a8af81c`: non-empty text rationale is required before a human override
can replace an AI label. Missing, null, empty, whitespace, numeric, list and object
values are rejected for both accept/reject judgments. Removing the guard caused
14 regression failures; after restoration, 32 cohort/evaluation tests and Ruff
passed. Valid human accept/reject paths and AI provenance remain covered.

The original 5,048-test full non-ML run is baseline evidence. It was not repeated
for this localized validation change; the 32-test gate verifies the new revision.
Extracted-wheel smoke verified the guard and disposable lifecycle, citations and
retirement. All 409 packaged source-file hashes match. No installation occurred.

Updated artifacts: `artifacts/e2e-fixes-20260907/release-20260908`.
Wheel SHA-256: `1c63432baffee46a04520f62af9b88bb562aae10618435ab8e14273d751f20c9`.
The adjacent manifest records code identity, focused versus baseline gates and
source archive identity. The original candidate remains available in `release`.

Next: the 13-item v1 human packet is ready for preliminary feedback, but cannot
satisfy the 20-review gate. A later non-overlapping cohort must supply enough
decisions and actual human reviews; do not pad or casually merge fingerprints.
Human acceptance, installation, restarts and historical recovery remain pending
their respective decisions. No quality thresholds, providers or prompts changed.

## Local review workspace - 2026-09-08

User authorized a page to read evidence, record judgments, save progress and
export JSONL for the existing evaluator. This implements that interaction; it
does not supply human judgments or activate/install the candidate.

- [x] Allocate disjoint work: Luna/high owns the browser template; Terra/high
  owns the validated packet builder, export contract and CLI; coordinator owns
  dashboard navigation/routing, integration and browser verification.
- [x] Build the accessible desktop/mobile page with cohort-bound local drafts.
- [x] Build a fingerprint-checked packet and self-contained HTML from frozen inputs.
- [x] Verify complete exports against the existing evaluator; incomplete/unsure
  reviews remain drafts and cannot count toward acceptance.
- [x] Verify authentication/Host gates, escaping, progress restore and JSONL export behavior.
- [x] Generate the actual preliminary 13-item review page and document handoff.

Data boundary: `/review` serves the empty UI behind existing dashboard policy;
it never selects a server-side cohort or exposes arbitrary local files. A local
packet is explicitly opened by the reviewer, or embedded in a generated HTML
file. Saved browser drafts contain judgments and reviewer identity, not source
excerpts. Downloading labels has no memory-promotion or claim-mutation effect.

Verification: 116 focused tests passed, including public lifecycle, cohort evaluator,
packet bindings, executable browser JavaScript, dashboard auth/Host/scopes and
architecture/release metadata. After final copy and responsive adjustments, the
17 page/packet/route tests passed again. Browser synthetic acceptance/rejection,
unsure and corrected-scope states, saved progress after reload and export status
were exercised. Mobile document width equals scroll width (341 CSS pixels).
The browser download event could not be retrieved by automation; export payloads
were verified by executable JavaScript tests. Direct file navigation was blocked
by browser policy; standalone HTML boot and escaping were tested with Node.
The actual 13-item HTML was regenerated from the final template without human
answers. No installed service was changed; disposable browser fixtures stopped.

## AI technical re-review - 2026-09-08

Operator correction: technical truth, freshness, duplication and usefulness review
belong to the agent. Completed AI-REREVIEW-13.md and machine-readable AI labels
against the same frozen 13 records, with current local source comparisons.
Result: 0/13 accepted as emitted; two semantic duplicate pairs. This supersedes
the earlier implication that these 13 were useful memories needing only human
sign-off. The old labels remain for audit; extractor behavior is not yet fixed.
No human review was fabricated, no thresholds changed, no authoritative claims
mutated and no runtime deployment performed.

## Useful-selection correction - source-verified candidate

Authority: ROADMAP.md, operator order 2026-09-08 to plan and implement E2E.
This supersedes the UI-first next step, not the earlier preserved test evidence.

- [x] Red witnesses: source-backed low-value add, rejected add still writes, old
  receipt promotion, wrong scope, semantic duplicate and useful positive control.
- [x] Versioned selection receipt in existing Dreaming consolidation, authoritative
  write and promotion checks, no active writes for reject/unknown.
- [x] Relevant authorized novelty context, source independence and replay checks.
- [x] Disposable E2E plus inverse mutations and separate actual-provider evaluation.
- [x] Focused regression, full non-ML, Ruff, release metadata, wheel/source handoff.

Ownership: coordinator implements core and roadmap; existing Terra/high performs
read-only security/promotion review; existing Luna/high analyzes balanced fixtures.
No parallel pytest: the shared test cleanup is unsafe across workers.

Implemented in `fea3ff6`. See [USEFUL-SELECTION.md](USEFUL-SELECTION.md) for the
final blinded 23-case comparison, original 13-record replay, full cost and limits.
The source tests retain positive useful outcomes as well as negative controls;
historical data and installed services have not been changed.

Final sequential gate: 5,118 passed, 75 skipped, 90 deselected, 1 xfailed,
17 warnings; native pytest exit 0, zero JUnit failures/errors. The PowerShell
wrapper surfaced stderr warnings as code 1; native exit was captured independently.
The wheel was built from an isolated commit archive, matches 411 packaged source
files and passes public retirement plus useful/rejected Dreaming lifecycles.
New artifacts: `artifacts/useful-selection-20260908/release`, including the final
source archive and `candidate-manifest.json`. Earlier candidate artifacts remain
unchanged. Installation, restart and historical curation are not part of this result.
