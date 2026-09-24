<!-- doc-head: September 8 installation and rollback procedure; executed delivery has a separate current record -->
Covers: seven remediation packages, useful-selection receipts, wheel identity and runtime proof.
Read before installing the September E2E candidate; use the adjacent implementation checklist for evidence.
Executed installation, client changes and restart are recorded in LIVE-DEPLOYMENT.md; restore/history stay separate.
ROADMAP.md remains the only roadmap; this document is an operator procedure.
<!-- /doc-head -->

# E2E candidate operator procedure

The operator subsequently requested operational completion. Current installed
artifact identity and execution results are recorded in
[LIVE-DEPLOYMENT.md](../.planning/audits/2026-09-07-e2e-review/LIVE-DEPLOYMENT.md).
The original source-candidate artifact references below remain historical.

The delivery record is [.planning/audits/2026-09-07-e2e-review/IMPLEMENTATION.md](../.planning/audits/2026-09-07-e2e-review/IMPLEMENTATION.md).
The original report preserves its pre-fix observations. Candidate artifacts live under
`artifacts/useful-selection-20260908/release`; `candidate-manifest.json` binds the source
commit, wheel SHA-256, package version and every packaged source file. A wheel
with the same version number is not proof of the same code: verify the hash.
Earlier candidates remain under `artifacts/e2e-fixes-20260907` for traceability.
This candidate includes useful-selection v2 and the rationale guard. Technical
selection evidence is in
[USEFUL-SELECTION.md](../.planning/audits/2026-09-07-e2e-review/USEFUL-SELECTION.md).
Final regression and package verification are recorded separately from installation.
The final non-ML gate passed 5,118 tests (native exit 0), and isolated wheel
verification matched 411 packaged source files and both useful/rejected lifecycles.

## Before an authorized installation

1. Review the candidate manifest, full non-ML result and bounded actual-provider
   comparison. The old frozen cohort has 70 AI decisions and zero human reviews;
   its historical human-provenance gate remains unsatisfied. The new technical
   selection evidence does not establish live production precision.
2. Identify the Python environment and only the processes that import it. Record
   the current package artifact and effective configuration, including Dreaming,
   observation/profile activation and the operational-review interval. Preserve
   current credentials and activation choices.
3. Retain the prior wheel/source revision and current scheduler configuration.
   Any production backup/restore requires its own authorization. If a live SQLite
   backup is approved, use SQLite's consistent backup mechanism and verify
   integrity/foreign keys; copying the main WAL database file alone is insufficient.
4. Compare the candidate SHA-256 with `Get-FileHash -Algorithm SHA256`. Review the
   source package changes independently if installation authority requires it.

## Installation, only after the later operator decision

Use the identified environment explicitly; the following placeholders are not
defaults or a command to run against an arbitrary Python:

```powershell
& $TargetPython -m pip install --no-deps --force-reinstall $VerifiedCandidateWheel
```

The package version remains 4.8.9 for this unpublished candidate. Verify installed
source hashes against the manifest, then restart only the affected dashboard/MCP
owners. Never kill all Python or stdio MCP processes to refresh one environment.
Keep the authenticated/legacy mode and tokens unchanged.

Reinstall the operational-review wrapper from the candidate source bundle using
the **existing** database, interval, canary, expected version and task name:

```powershell
& .\scripts\install-windows-operational-review.ps1 `
  -PythonExe $TargetPython -Database $ExistingDatabase `
  -ExpectedVersion '4.8.9' -EveryHours $ExistingEveryHours `
  -LookbackHours $ExistingLookbackHours -CanaryQuery $ExistingCanaryQuery `
  -CanaryHumanId $ExistingCanaryHumanId -TaskName $ExistingTaskName
```

This registers/replaces a task and schedules its first run about two minutes
later. It is a deployment action, not a harmless installer check. The task uses
priority 6 (normal I/O); the internal supervisor deadline is 24 minutes and the
scheduler limit is 25 minutes. The observed priority-7 slowdown and final
naturally scheduled PASS are recorded in LIVE-DEPLOYMENT.md.
`latest.json` remains the last complete review; `attempt.json` describes the
newest attempt. The dashboard reads the installer result directory by default;
`MEMORYMASTER_REVIEW_RESULTS` can explicitly select another result directory.

## Expected behavior and runtime proof

- Dreaming's auxiliary ledger migration adds versioned resume eligibility,
  extraction-run lineage and a deferral reason. New extraction and eligibility
  are saved atomically. Existing extracted rows remain in historical retention.
  Resume uses the saved extraction and existing leases, budgets and idempotency.
  Do not enable disabled Dreaming or replay retained history to prove this fix.
- New Dreaming retention requires an accepted useful-selection v2 receipt.
  Reject/unknown writes no active candidate; global/relay attribution cannot
  grant authority. Novelty uses authorized references without access reinforcement.
  Old or changed receipts cannot authorize a new Steward/skill confirmation.
  Pending old decisions are re-reviewed on retry; existing confirmed history is
  preserved. Do not curate historical claims as an implicit installation step.
- Recall budgets apply to the serialized **context**, including framing and
  derived sections. Receipts contain only delivered claims/IDs/citations, with
  `budget_scope="context"` and a separate canonical application-JSON estimate.
  Transport wrappers are excluded. Budgets below the minimum representation
  raise a validation error. Text/XML/JSON names and public v1 remain unchanged.
- Dashboard Origin/Referer/Host checks use exact normalized authorities and the
  bound port. Wildcard binds require explicit
  `MEMORYMASTER_DASHBOARD_ALLOWED_ORIGINS` (comma-separated scheme/host/port).
  Proxy headers do not authorize destinations. Loopback aliases are automatic
  only for loopback binds. Local non-browser clients may omit Origin/Referer,
  but must still send an allowed Host.
- The decision summary restricts capture counters to the authorized requested
  scope. Global profile/review data are unavailable when attribution would cross
  that boundary. Search excludes candidates, stale and conflicted claims by
  default; diagnostic APIs retain explicit state options. Empty scoped results
  never retry against all scopes.
- `MEMORYMASTER_MCP_TOOL_PROFILE=full|core` is opt-in **discovery** configuration.
  `full` is default. `core` advertises remember/recall/forget/improve; named legacy
  adapter calls remain governed by the same policies. Hidden tools are not
  permission-revoked, and advertised tools are not permission-granted. Hermes
  still uses its authorized `forget_preview` and session-scope calls. Invalid
  profile values fail startup. Do not alter installed Hermes config for this
  experiment without a later decision.

After installation, prove the disposable candidate -> confirmed/cited -> retired
journey in the installed environment. Then observe an actual naturally scheduled
review: match the attempt ID, complete artifact, seven phase timings, installed
source hashes and fresh completion. An installer exit code, task registration,
bridge delivery or an older PASS is not that proof. No peer pane means no peer
checkpoint completion.

## Historical human-provenance workflow

This preserved workflow is not the next technical QA task for the operator.
The agent-owned technical re-review rejected all 13 original records as emitted,
and selection v2 was checked separately with useful positive controls.
The immutable local cohort is `artifacts/e2e-fixes-20260907/cohort-v1`, with its
manifest/cutoff, redacted source evidence, `ai-labels.jsonl` and
`human-review-template.jsonl`. Any genuine human judgments use a separate file
with actual reviewer, rationale, acceptance, record IDs and cohort fingerprint.
AI labels must not be relabeled as human. Exact quote match, semantic support,
current validity, scope and usefulness are separate judgments. The existing
sampler/evaluator/thresholds remain authoritative.
Missing, blank, whitespace-only and non-text human rationales are rejected and
do not count toward human-review totals. The existing 13-item packet cannot
satisfy the 20-review gate, and no such human review has been fabricated.

```powershell
python scripts/evaluate_dreaming.py $AiLabels --cohort $FrozenCohort --human-labels $HumanLabels
```

Exit 3 means pending/failed acceptance, not an execution failure. Only 13 emitted
decisions exist in v1, so even all 13 human reviews cannot meet the 20-review
minimum. Freeze a later non-overlapping cohort once sufficient emissions exist;
do not edit v1 or pad its sample with invented emissions. The usage report covers
all provider calls in the window, including rejected work. It does not estimate
saved money or attribute all tokens to useful memory.

## Rollback without deleting evidence

Return the package to the retained pre-candidate wheel or revert an individual
package commit in a fresh review branch, then repeat the relevant regression
gate. Restore the captured scheduler configuration when rolling back supervision.
Restart only its known owners. Set the discovery profile to `full` to disable
that experiment; no data migration is needed.

**Keep the additive Dreaming columns and migration record.** Older readers ignore
them and retain their original selectors. Do not drop/recreate the ledger, delete
extractions or turn historical eligibility on during rollback. Returning to old
code also returns its lack of automatic resume. Inventory retained history only:

```powershell
python -m memorymaster.dreaming.history_inventory --ledger $ExistingCaptureLedger --scope $AuthorizedScope
```

The inventory is query-only and reports IDs, age, origin, quantities and
fingerprints. It contains no bulk-apply command. Production restore and selective
historical recovery remain separate future decisions.
