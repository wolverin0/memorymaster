<!-- doc-head: useful-selection source and provider evidence; subsequent live deployment recorded separately -->
Covers: v2 retention receipts, authorized novelty, replay, promotion and actual Gemini selection checks.
Read when reviewing the candidate or changing the selection prompt; ROADMAP.md remains the sole roadmap.
Evidence separates fixture plumbing and frozen provider replay from the linked LIVE-DEPLOYMENT.md record.
The historical 13 records and earlier AI/human provenance metrics remain unchanged in authoritative storage.
<!-- /doc-head -->

# Useful-selection correction

Subsequent authorized installation and runtime evidence are in
[LIVE-DEPLOYMENT.md](LIVE-DEPLOYMENT.md). The candidate-only delivery statements
below describe the original source checkpoint, before that operational request.

The operator's September 8 correction supersedes the previous UI-first acceptance
step. A supported statement is not automatically worth retaining. Technical
truth, chronology, duplication and marginal usefulness are agent-owned checks;
the operator is not assigned technical QA of these 13 records.

## Implemented behavior

The existing consolidation call classifies destination, knowledge kind, novelty,
exact scope, a proposition key and concrete future use. An accepted v2 receipt
requires all eight evidence/selection checks. Only stable useful memory can be
added or proposed; rejection and uncertainty remain in the capture ledger and
write no active candidate or lifecycle proposal. Account/access configuration
belongs in current project documentation, even when intentionally configured to
save automation effort. A lasting security policy is a separate useful constraint.

Global transport attribution cannot authorize a project claim. Generated summaries
and relays do not supply independent source authority. The receipt binds the
source context, unchanged claim fields, scope and exact citation. Storage,
validator, LLM Steward and skill approval cannot newly confirm a Dreaming claim
with a missing, old, rejected or modified receipt. Existing confirmed historical
rows are preserved; this change does not curate or withdraw them automatically.

Novelty comparison retrieves relevant authorized older claims before recent
claims, without recording access or confidence reinforcement. References and
proposal targets enforce exact tenant, scope, visibility and requesting-agent
boundaries. Later batches see earlier same-scope reviewed candidates. Duplicate
keys, normalized claims and equivalent triples in the bounded authorized
comparison context prevent a second active write.
Repeated statements cannot become independent reinforcement. Updates remain
explicit proposals rather than silently replacing current claims.

Retries reuse their own idempotent candidate and restore its missing receipt after
an interruption. They do not revive archived/stale work. Pending legacy reviews
are reconsolidated on retry without re-extracting the source. Proposal targets are
authorized before candidate creation. Invalid metadata on an explicit `ignore`
decision cannot block another candidate whose complete acceptance receipt passes.

## Verification boundaries

Disposable integration tests exercise capture -> extraction -> consolidation ->
candidate -> Steward -> cited recall -> retirement, including rejected sources,
wrong scope, missing/modified review, replay, duplicate batches and inaccessible
proposal targets. Their provider verdicts are explicitly synthetic orchestration
fixtures, not estimates of model quality.

The provider experiment freezes candidate inputs and expected judgments before
each run. It compares prior review instructions from `13dbf9a` with selection v2
through the existing Gemini OAuth client. Case/message IDs are opaque and cases
are ordered by their hashes; expected labels and reasons are never in the model
prompt. The fresh positive rule uses a separate project from the duplicate-rule
scenario, so their current-memory contexts cannot contaminate each other.

Final blinded comparison: 23 cases, nine useful and fourteen negative, across
nine scope batches per arm, using `gemini-3.7-flash-low`.

| Selection instructions | Useful retained | Incorrectly retained | Useful missed | Negatives discarded | Precision | Recall |
|---|---:|---:|---:|---:|---:|---:|
| Prior `13dbf9a` | 9 | 3 | 0 | 11 | 75% | 100% |
| Current v2 | 9 | 0 | 0 | 14 | 100% | 100% |

The prior arm admitted two duplicates and the access-configuration case. Candidate
actions are measured before Steward promotion; prior `reinforce` actions count
because that worker path wrote a candidate. Both arms used nine recorded calls.
Prior instructions consumed 162,018 input / 5,011 output tokens; v2 consumed
176,095 input / 7,578 output tokens (8.7% more input, no extra call per batch).
Unchanged prompt/model responses were reused when isolating the fresh-rule
scenario; changed batches were called again. The report marks reused responses.

This is a development set, not a held-out benchmark or a production precision estimate.
It tests selection over frozen candidates; it does not measure whether a live
extractor finds every useful statement in arbitrary new conversations.
Semantic usefulness and paraphrase matching remain model judgments, and bounded
retrieval is not an exhaustive duplicate search over every historical claim.

Separately, the final prompt replayed all 13 original redacted emissions in four
scope batches: 13 explicit ignores, zero writes and zero validation errors.
That replay used 116,411 input and 4,385 output tokens. It did not read or mutate
the authoritative database. Original rows remain untouched.

Earlier exploratory rounds are retained locally. One exposed account access
configuration being admitted as a durable decision; another exposed malformed
ignore metadata blocking an otherwise valid batch. Those failures caused the
corrections above. Exploratory results with descriptive case IDs are not the
final blinded comparison.

Total completed provider work across exploratory and final rounds was 51 unique
calls, 1,052,168 input and 53,034 output tokens. Cached duplicates are excluded
by provider conversation identity. Those costs include diagnostic rounds and
are not the recurring cost of a Dreaming cycle.

## Reproduce the bounded check

The normal test suite checks public synthetic model receipts and their exact
prompt fingerprints without network calls. A prompt/input change requires fresh
evidence; a matching hash can reuse an unchanged response.

```powershell
python -m pytest tests/test_dreaming_selection_provider.py -q
python scripts/evaluate_dreaming_selection.py --output artifacts/selection-review-new --write-receipts tests/fixtures/dreaming_selection_provider_v2.json
```

The second command makes bounded actual-provider calls using the existing CLI
session, with no claims database. Add `--baseline-ref 13dbf9a` for a paired prior
instruction comparison; the revision must exist in the local Git history.
`--reuse <provider-results.json>` reuses only identical prompt/model responses.
The command fails on missing coverage, validation errors or any unexpected v2
outcome and writes regression receipts only when every expected outcome matches.

## Delivery

Implementation commit: `fea3ff6`. The 160 focused Dreaming/public-demo tests passed;
after extracting two bounded worker helpers, the 46 affected worker/selection
tests passed again. Ruff and generated release facts passed. The skill-promotion
test failed with its guard removed and passed after restoration. Rejected writes,
interrupted receipt recovery, invalid target writes and inaccessible private/
sensitive targets each had a demonstrated failing regression before correction.

The wheel was built from an isolated archive of `fea3ff6`, and all 411 packaged
source files match that archive. A fresh isolated process imported the extracted
wheel and passed the public cited-recall/source-retirement demo plus useful and
rejected Dreaming lifecycle cases. The package remains an unpublished 4.8.9
candidate, identified by SHA-256 rather than its version number:
`8e6d4ad5eb549c2c41184579ae252d833218ae3c3d13d6a2d244416adac94559`.

Final sequential non-ML regression: **5,118 passed, 75 skipped, 90 deselected,
1 xfailed, 17 warnings**, in 1,221.63 seconds. JUnit reports zero failures/errors,
and a read-only handle to the native pytest process recorded exit **0**.
PowerShell rendered startup stderr warnings as `NativeCommandError` and returned
wrapper code 1; that wrapper result is not substituted for the native process
and JUnit evidence. The final source gate was run after all code changes.

Artifacts are under `artifacts/useful-selection-20260908/release`: the wheel,
final source archive, `wheel-smoke.json` and `candidate-manifest.json`. The manifest
binds code/delivery commits, both archives, wheel SHA-256, packaged-source hashes,
native exit/JUnit evidence and provider metrics. Headers and local links for all
five changed delivery documents were checked.

Installation, service restarts, natural scheduler
verification and historical curation are separate unexecuted actions. The old
cohort's missing human-provenance gate remains honestly pending; no human labels
are fabricated, and it is not relabeled as current production quality acceptance.
