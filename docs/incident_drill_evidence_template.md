# Incident Drill Evidence and Signoff Template (D7)

Use this template after each drill run from `docs/incident_drill_checklist.md`.

## Drill Metadata

- drill_id:
- date_utc:
- environment:
- operator:
- db_path:
- workspace_root:

## Scope and Scenario

- scenario_name:
- trigger:
- expected_outcome:

## Commands Executed

```text
# paste exact commands in execution order
```

## Artifact Evidence

- incident_drill_run_json:
- incident_drill_evidence_md:
- command_logs_dir:
- perf_smoke_json:
- eval_results_json:
- eval_report_md:
- eval_checks_csv:
- reconciliation_report_before:
- reconciliation_fix_report:
- reconciliation_report_after:
- operator_e2e_report_json:
- additional_artifacts:

## Reconciliation Before/After

| Metric | Before | After | Notes |
| --- | --- | --- | --- |
| orphan_events |  |  |  |
| orphan_citations |  |  |  |
| hash_chain_issues |  |  |  |
| superseded_without_replacement |  |  |  |
| invalid_transitions |  |  |  |

## Performance Summary

| Metric | Threshold | Observed | Pass |
| --- | --- | --- | --- |
| ingest_p95_ms | <= 60 |  |  |
| ingest_ops_per_sec | >= 80 |  |  |
| query_p95_ms | <= 250 |  |  |
| query_ops_per_sec | >= 12 |  |  |
| cycle_p95_seconds | <= 3.5 |  |  |
| wall_clock_seconds | <= 20 |  |  |

## Findings and Remediation

| Finding | Severity | Owner | Due Date | Status |
| --- | --- | --- | --- | --- |
|  |  |  |  |  |

## Signoff Checklist

- [ ] Health/API reachable and evidence captured.
- [ ] Perf smoke met thresholds or approved exception documented.
- [ ] Strict eval passed or approved exception documented.
- [ ] Reconciliation before/after captured with no unresolved critical issues.
- [ ] Operator restart behavior validated and recorded.
- [ ] All remediation items have owners and due dates.

## Approvals

- ops_reviewer_name:
- ops_reviewer_date_utc:
- engineering_owner_name:
- engineering_owner_date_utc:
- release_manager_name:
- release_manager_date_utc:
