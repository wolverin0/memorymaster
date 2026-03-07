# Incident Drill Checklist (D6)

Run this checklist as a recurring reliability drill for MemoryMaster operations.
Record evidence in `docs/incident_drill_evidence_template.md`.

Primary automation command:

```powershell
python scripts/run_incident_drill.py --db memorymaster.db --workspace .
```

Recurring automation (hourly example, stop on first failure, optional webhook alert):

```powershell
python scripts/recurring_incident_drill.py --db memorymaster.db --workspace . --interval-seconds 3600 --max-runs 24 --stop-on-fail --notify-webhook-env MEMORYMASTER_DRILL_WEBHOOK
```

## Scope

Drill validates:
- ingest/query/cycle operability
- event integrity chain continuity
- orphan/suspicious data detection and repair workflow
- recovery from operator interruption

## Preconditions

1. Access to DB and artifacts directory.
2. Ability to run:
   - `python scripts/eval_memorymaster.py --strict`
   - `python benchmarks/perf_smoke.py`
   - `python scripts/run_incident_drill.py --dry-run`
3. Test inbox JSONL for operator smoke.

## Drill Steps

1. Baseline health
   - start dashboard/API
   - verify `/health` returns ok

2. Functional smoke
   - ingest sample claims with citations
   - run one cycle
   - query expected values

3. Performance gate
   - run `python benchmarks/perf_smoke.py`
   - confirm pass and store `artifacts/perf/perf_smoke.json`

4. Robustness gate
   - run `python scripts/eval_memorymaster.py --strict`
   - confirm artifact generation

5. Integrity reconciliation (report mode)
   - call `reconcile_integrity(fix=False)`
   - record summary counts and issue categories

6. Integrity reconciliation (controlled fix)
   - if orphan/hash findings are non-zero, run `reconcile_integrity(fix=True)`
   - rerun `reconcile_integrity(fix=False)` and verify expected reductions

7. Operator restart behavior
   - run operator with state enabled
   - interrupt process
   - restart and confirm checkpoint resume

8. Evidence and signoff
   - complete all sections in `docs/incident_drill_evidence_template.md`
   - ensure all signoff checklist items are checked
   - collect reviewer/owner approvals

## Automated Artifact Flow

`scripts/run_incident_drill.py` creates a timestamped drill folder under:

- `artifacts/incident_drill/<drill_id>/`

Generated artifacts include:

- `incident_drill_run.json` (top-level run status + gate outcomes)
- `incident_drill_evidence.md` (prefilled evidence draft for signoff workflow)
- `commands/*.stdout.txt` and `commands/*.stderr.txt` (per-command logs)
- `perf/perf_smoke.json`
- `eval/eval_results.json`
- `eval/eval_report.md`
- `eval/eval_checks.csv`
- `reconcile/reconcile_before.json`
- `reconcile/reconcile_fix.json`
- `reconcile/reconcile_after.json`
- `operator/operator_e2e_report.json` (when operator e2e step is enabled)
- `../recurring_history.jsonl` and `../recurring_latest.json` (when using recurring scheduler)

## Pass/Fail Criteria

Pass:
- health check reachable
- perf smoke passes configured thresholds
- strict evaluation passes
- reconciliation report has no unresolved critical orphan/hash issues after fix workflow
- operator resume confirmed

Fail:
- any gate above fails
- unresolved integrity criticals remain
- artifacts missing for audit trail

## Post-Drill Output

Capture:
- drill timestamp and environment
- command log
- artifact paths
- reconciliation before/after summaries
- follow-up remediation items with owners and due dates
- completed evidence template with approvals
