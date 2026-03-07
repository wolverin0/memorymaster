# Deployment Profiles (D4)

This document defines practical deployment profiles for MemoryMaster with reliability/operability defaults.

## Profile A: Local Developer (SQLite)

Use when:
- single developer
- local experiments, test loops, feature validation

Runtime:
- DB: local SQLite file (`memorymaster.db`)
- process: CLI/operator loop on same host

Recommended commands:
```powershell
python -m memorymaster --db memorymaster.db init-db
python -m memorymaster --db memorymaster.db run-operator --inbox-jsonl .\turns.jsonl --retrieval-mode hybrid --policy-mode cadence --max-idle-seconds 120 --log-jsonl artifacts\operator\operator_events.jsonl
```

Reliability notes:
- keep operator checkpoints enabled (`artifacts/operator/operator_state.json`)
- run synthetic eval + perf smoke before sharing changes:
```powershell
python scripts/eval_memorymaster.py --strict
python benchmarks/perf_smoke.py
```

## Profile B: Small Team Server (SQLite or Postgres)

Use when:
- 2-10 engineers
- one shared service host
- moderate throughput

Runtime:
- preferred DB: Postgres for concurrency/durability
- fallback DB: SQLite when write contention is low
- process model: one long-running operator, optional dashboard process

Baseline controls:
- backup policy for DB and artifacts
- health endpoint checks (`/health`)
- periodic reconciliation report:
  - `service.store.reconcile_integrity(fix=False)` in scheduled job

## Profile C: Cloud Managed DB (Postgres)

Use when:
- production workloads
- multi-service/multi-agent concurrency
- stricter recovery and audit requirements

Runtime:
- DB: managed Postgres with TLS + backups + PITR
- application nodes: stateless operator workers
- observability: centralized logs + metric exporter (future D3)

Recommended controls:
- rotate credentials and isolate DB role permissions
- scheduled integrity reconciliation (`report` daily, `fix` only with review)
- retain artifacts for audit windows (`artifacts/eval`, `artifacts/perf`, `artifacts/e2e`)

## Rollout Checklist

1. Validate DB connectivity and schema init.
2. Run operator smoke against representative inbox rows.
3. Run `scripts/eval_memorymaster.py --strict`.
4. Run `benchmarks/perf_smoke.py`.
5. Capture reconciliation report and confirm zero critical findings before go-live.
