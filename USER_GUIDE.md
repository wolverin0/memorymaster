# MemoryMaster User Guide

This guide explains how to run MemoryMaster end-to-end: store facts, keep them updated, connect to LLM tooling through MCP, and measure reliability.

## 1) What It Does

MemoryMaster is a memory layer for LLM workflows:
- captures facts as structured claims with citations
- tracks lifecycle (`candidate`, `confirmed`, `stale`, `superseded`, `conflicted`, `archived`)
- re-validates memory over time
- retrieves relevant memory before new turns
- provides audit artifacts for evaluation

## 2) Install

```powershell
pip install -e .
```

Optional extras:

```powershell
pip install -e ".[mcp]"
pip install -e ".[postgres]"
pip install -e ".[security]"
```

## 3) Quick Start (5 minutes)

```powershell
python -m memorymaster --db memorymaster.db init-db
python -m memorymaster --db memorymaster.db ingest --text "Support email is help@example.com" --source "session://chat|turn-1|user provided"
python -m memorymaster --db memorymaster.db run-cycle --policy-mode cadence
python -m memorymaster --db memorymaster.db query "support email" --retrieval-mode hybrid
```

Retry-safe ingest example (same `idempotency_key` will not duplicate rows):

```powershell
python -m memorymaster --db memorymaster.db ingest --text "Support email is help@example.com" --source "session://chat|turn-1|value" --idempotency-key support-email-turn-1
```

## 4) Main Runtime Pattern (Operator Loop)

Use `run-operator` for continuous pre-turn retrieval + post-turn maintenance.

```powershell
python -m memorymaster --db memorymaster.db run-operator --inbox-jsonl .\turns.jsonl --retrieval-mode hybrid --policy-mode cadence --max-idle-seconds 120 --log-jsonl artifacts\operator\operator_events.jsonl
```

Recommended safeguards:
- `--max-idle-seconds 120` prevents endless loops when input stops.
- `--max-events N` bounds one run for deterministic tests/CI.
- default checkpoint `artifacts/operator/operator_state.json` plus durable queue state/journal (`artifacts/operator/operator_queue_state.json`, `artifacts/operator/operator_queue_journal.jsonl`) allows restart resume with pending work preserved.

Checkpoint controls:
- custom path: `--state-json <path>`
- custom durable queue paths: `--queue-state-json <path>`, `--queue-journal-jsonl <path>`
- disable checkpoints: `--no-state`

## 4.1) Read-Only Dashboard and API

Run dashboard server:

```powershell
python -m memorymaster --db memorymaster.db run-dashboard --host 127.0.0.1 --port 8765
```

Quick status (2026-03-03): A1, A2, A11, and C1 are complete.

Alternative script entrypoint:

```powershell
memorymaster-dashboard --db memorymaster.db --host 127.0.0.1 --port 8765
```

Useful URLs:
- `http://127.0.0.1:8765/dashboard`
- `http://127.0.0.1:8765/health`
- `http://127.0.0.1:8765/api/claims?limit=50`
- `http://127.0.0.1:8765/api/events?limit=100`
- `http://127.0.0.1:8765/api/timeline?limit=100`
- `http://127.0.0.1:8765/api/conflicts?limit=50`
- `http://127.0.0.1:8765/api/review-queue?limit=100`
- `http://127.0.0.1:8765/api/operator/stream?last=20`

Dashboard review-queue rows support triage actions:
- `pin`, `mark_reviewed`, `suppress`
- `approve_proposal`, `reject_proposal` (resolve latest pending steward proposal for the claim)

## 4.2) Steward Loop (Probe + Proposal Pass)

Run a stewardship pass that plans claim-level probes (`filesystem_grep`, `deterministic_format`, `deterministic_citation_locator`, `semantic_probe`, `tool_probe`), produces machine-readable decisions (`keep`, `stale`, `conflicted`, `superseded_candidate`), and writes a report artifact:

```powershell
python -m memorymaster --db memorymaster.db --workspace . run-steward --mode manual --max-cycles 1 --max-claims 200 --max-tool-probes 200 --probe-timeout-seconds 2 --probe-failure-threshold 3 --artifact-json artifacts\steward\steward_report.json
```

Cadence mode:

```powershell
python -m memorymaster --db memorymaster.db --workspace . run-steward --mode cadence --interval-seconds 60 --max-cycles 10 --disable-semantic-probe
```

Commit-aware cadence example:

```powershell
python -m memorymaster --db memorymaster.db --workspace . run-steward --mode cadence --cadence-trigger timer_or_commit --interval-seconds 3600 --git-check-seconds 10 --commit-every 1 --max-cycles 20
```

Default behavior is non-destructive and emits review-queue style proposal events (`policy_decision`) for human override. Add `--apply` to perform status transitions from steward decisions.

List and resolve steward proposals:

```powershell
# list pending proposals
python -m memorymaster --db memorymaster.db --workspace . steward-proposals --limit 50

# approve latest pending proposal for claim 42 (applies transition by default)
python -m memorymaster --db memorymaster.db --workspace . resolve-proposal --action approve --claim-id 42

# reject proposal event 123
python -m memorymaster --db memorymaster.db --workspace . resolve-proposal --action reject --proposal-event-id 123
```

## 5) Input Formats for `turns.jsonl`

One JSON object per line. Supported shapes:

Explicit turn fields:
```json
{"session_id":"s1","thread_id":"t1","turn_id":"turn-001","user_text":"Deploy to staging","assistant_text":"Starting deployment now.","observations":["CI green"]}
```

`events` format:
```json
{"session_id":"s1","thread_id":"t1","events":[{"role":"user","text":"Deadline is 2026-05-01"},{"role":"assistant","text":"Saved"}]}
```

`messages` format:
```json
{"session_id":"s1","thread_id":"t1","messages":[{"role":"user","content":"Database host is db.internal"},{"role":"assistant","content":"Noted"}]}
```

Privacy note:
- text inside `<private>...</private>` is ignored for extraction/ingestion.

## 6) MCP Integration (Codex/Claude-Style Tooling)

Install MCP extra:

```powershell
pip install -e ".[mcp]"
```

Start server:

```powershell
memorymaster-mcp
```

MCP config example:

```json
{
  "mcpServers": {
    "memorymaster": {
      "command": "memorymaster-mcp"
    }
  }
}
```

Core MCP tools:
- `init_db`
- `ingest_claim`
- `run_cycle`
- `run_steward`
- `query_memory`
- `list_claims`
- `redact_claim_payload`
- `pin_claim`
- `compact_memory`
- `list_events`
- `open_dashboard`
- `list_steward_proposals`
- `resolve_steward_proposal`

`ingest_claim` supports optional `idempotency_key` for retry-safe writes.

Concrete command examples:

```powershell
# CLI list-events
python -m memorymaster --db memorymaster.db list-events --claim-id 42 --limit 50
```

```text
# MCP open_dashboard
mcp__memorymaster__open_dashboard {"check_health":true}

# MCP list_events
mcp__memorymaster__list_events {"claim_id":42,"limit":50}

# MCP list pending steward proposals
mcp__memorymaster__list_steward_proposals {"limit":50}

# MCP redact claim + citations non-destructively (audit event emitted)
mcp__memorymaster__redact_claim_payload {"claim_id":42,"mode":"redact","redact_claim":true,"redact_citations":true,"reason":"ticket-123"}

# MCP run steward once (non-destructive)
mcp__memorymaster__run_steward {"mode":"manual","max_cycles":1,"max_claims":200,"max_tool_probes":200}

# MCP reject latest pending proposal for claim 42
mcp__memorymaster__resolve_steward_proposal {"action":"reject","claim_id":42}
```

## 7) Connectors and Scheduled Ingest

Convert local exports into operator-ready rows:

```powershell
python scripts/git_to_turns.py --input .\git_export.json --output .\turns_git.jsonl --session-id git --thread-id repo-main
python scripts/tickets_to_turns.py --input .\tickets_export.json --output .\turns_tickets.jsonl --session-id tickets --thread-id ops-board
python scripts/messages_to_turns.py --input .\messages_export.json --output .\turns_messages.jsonl --session-id messages --thread-id inbox-main
python scripts/conversation_importer.py --input .\conversation_export.json --output .\turns_conversation.jsonl --format auto --session-id import --thread-id thread-main
python scripts/jira_live_to_turns.py --input .\jira_live_config.json --output .\turns_jira_live.jsonl --cursor-json artifacts\connectors\jira_live_cursor.json
python scripts/slack_live_to_turns.py --input .\slack_live_config.json --output .\turns_slack_live.jsonl --cursor-json artifacts\connectors\slack_live_cursor.json
python scripts/email_live_to_turns.py --input .\email_live_config.json --output .\turns_email_live.jsonl --cursor-json artifacts\connectors\email_live_cursor.json
```

Run operator on a generated inbox:

```powershell
python -m memorymaster --db memorymaster.db run-operator --inbox-jsonl .\turns_messages.jsonl --retrieval-mode hybrid --policy-mode cadence --max-idle-seconds 120
```

Run repeated import + ingest with deterministic idempotency keys and incremental cursor state:

```powershell
python scripts/scheduled_ingest.py --db memorymaster.db --connector messages --input .\messages_export.json --turns-output artifacts\connectors\messages_turns.jsonl --interval-seconds 60 --max-runs 10 --cursor-limit 10000
```

For connectors with person/contact data, set explicit sensitivity handling:

```powershell
python scripts/scheduled_ingest.py --db memorymaster.db --connector messages --input .\messages_export.json --sensitivity-mode redact --once
```

`scheduled_ingest.py` connectors include `conversation`, `git`, `tickets`, `messages`, `github_live`, `jira_live`, `slack_live`, `email_live`, and `webhook`.

Connector scripts document accepted JSON/JSONL export shapes in their file headers.

## 8) Review and Curation Workflow

Inspect claims:

```powershell
python -m memorymaster --db memorymaster.db list-claims
```

Inspect events:

```powershell
python -m memorymaster --db memorymaster.db list-events --limit 100
```

Generate curation queue (stale/conflicted priority):

```powershell
python -m memorymaster --db memorymaster.db review-queue --limit 100
```

Pin critical facts:

```powershell
python -m memorymaster --db memorymaster.db pin 42
```

Redact or erase a claim/citation payload non-destructively (audit event emitted):

```powershell
python -m memorymaster --db memorymaster.db redact-claim 42 --mode redact --reason "ticket-123"
```

## 8.1) Compaction Traceability Artifacts

Run compaction:

```powershell
python -m memorymaster --db memorymaster.db compact --retain-days 30 --event-retain-days 60
```

Each compaction run emits artifacts in `artifacts/compaction/` (relative to workspace root):
- `summary_graph.json` (summary/claim/citation graph)
- `traceability.json` (summary-to-source and claim-to-citation lineage)

The compaction API response remains backward-compatible (`archived_claims`, `deleted_events`), while artifact paths are recorded in the `compaction_run` event payload.

Render a markdown report from artifacts:

```powershell
python scripts/compaction_trace_report.py --artifacts-dir artifacts/compaction --out-md artifacts/compaction/compaction_trace_report.md
```

Validate compaction artifact integrity (summary graph + traceability cross-checks):

```powershell
python scripts/compaction_trace_validate.py --artifacts-dir artifacts/compaction --out-json artifacts/compaction/compaction_trace_validation.json
```

## 9) Evaluation and Robustness

Synthetic benchmark:

```powershell
python scripts/eval_memorymaster.py --strict
```

Perf smoke SLO gate (fast CI-friendly synthetic run):

```powershell
python benchmarks/perf_smoke.py --slo-config benchmarks/slo_targets.json
```

Current SLO targets are configured in `benchmarks/slo_targets.json` and enforced by `benchmarks/perf_smoke.py`:
- ingest latency `p95 <= 60ms` and throughput `>= 80 ops/sec`
- query latency `p95 <= 250ms` and throughput `>= 12 ops/sec`
- query misses on known keys `= 0`
- cycle latency `p95 <= 3.5s`
- full run wall-clock `<= 20s`

CI runs both `quick` and bounded `production` profile perf gates.

Run a sustained profile from the same config:

```powershell
python benchmarks/perf_smoke.py --slo-config benchmarks/slo_targets.json --profile sustained --claims 400 --queries 120 --cycles 6
```

Run a production profile from the same config:

```powershell
python benchmarks/perf_smoke.py --slo-config benchmarks/slo_targets.json --profile production --claims 220 --queries 80 --cycles 4
```

You can override individual thresholds at runtime (for example `--query-p95-max 0.30`) without replacing the full config file.

Deterministic operator end-to-end harness:

```powershell
python scripts/e2e_operator.py
```

Automated incident drill runner (D6):

```powershell
python scripts/run_incident_drill.py --db memorymaster.db --workspace .
```

Drill with signoff artifact generation (approver values can also come from environment variables):

```powershell
python scripts/run_incident_drill.py --db memorymaster.db --workspace . --approver-name "Ops Reviewer" --approver-email "ops@example.com" --approver-role "SRE" --approval-ticket "CHG-1234" --signing-key-env MEMORYMASTER_DRILL_SIGNING_KEY
```

Enforce strict signoff approval completion, and optionally require an HMAC-signed signoff:

```powershell
python scripts/run_incident_drill.py --db memorymaster.db --workspace . --strict-drill-signoff-complete --strict-drill-signoff-signed --signing-key-env MEMORYMASTER_DRILL_SIGNING_KEY --approver-name "Ops Reviewer" --approver-email "ops@example.com" --approver-role "SRE" --approval-ticket "CHG-1234" --approver-decision approved
```

Strict mode for compaction artifact presence/integrity:

```powershell
python scripts/run_incident_drill.py --db memorymaster.db --workspace . --strict-compaction-trace-validation
```

Plan-only dry run:

```powershell
python scripts/run_incident_drill.py --dry-run
```

Recurring drill scheduler with optional webhook alert on failure:

```powershell
python scripts/recurring_incident_drill.py --db memorymaster.db --workspace . --interval-seconds 3600 --max-runs 24 --stop-on-fail --notify-webhook-env MEMORYMASTER_DRILL_WEBHOOK
```

Recurring scheduler with forwarded signoff metadata:

```powershell
python scripts/recurring_incident_drill.py --db memorymaster.db --workspace . --interval-seconds 3600 --max-runs 24 --stop-on-fail --notify-webhook-env MEMORYMASTER_DRILL_WEBHOOK --approver-name "Ops Reviewer" --approver-email "ops@example.com" --approver-role "SRE" --approval-ticket "CHG-1234" --signing-key-env MEMORYMASTER_DRILL_SIGNING_KEY
```

Recurring scheduler forwarding strict signoff gates (`history` rows and failure webhook payload include resulting signoff gate fields):

```powershell
python scripts/recurring_incident_drill.py --db memorymaster.db --workspace . --interval-seconds 3600 --max-runs 24 --stop-on-fail --notify-webhook-env MEMORYMASTER_DRILL_WEBHOOK --strict-drill-signoff-complete --strict-drill-signoff-signed --signing-key-env MEMORYMASTER_DRILL_SIGNING_KEY
```

Operator metrics from event logs (D3 structured exporter):

```powershell
python -m memorymaster export-metrics --events-jsonl artifacts/operator/operator_events.jsonl --out-prom artifacts/metrics/memorymaster.prom --out-json artifacts/metrics/memorymaster_metrics.json
```

You can pass multiple event files by repeating `--events-jsonl`.
The exporter emits counters for events/transitions/status values plus latency `p50`/`p95` for `ingest`, `query`, `cycle`, and `operator_turn` when present in the data.

Operator stream metrics aggregator (queue and error health + latency summaries):

```powershell
python scripts/operator_metrics.py --events-jsonl artifacts/operator/operator_events.jsonl --out-json artifacts/e2e/operator_metrics.json
```

Alert gate for operator metrics (non-zero exit on breach, optional webhook):

```powershell
python scripts/alert_operator_metrics.py --metrics-json artifacts/e2e/operator_metrics.json --queue-max 5 --error-max 0 --p95-max-ms operator_turn=500 --webhook-env MEMORYMASTER_ALERT_WEBHOOK
```

Useful artifacts:
- `artifacts/eval/eval_results.json`
- `artifacts/eval/eval_report.md`
- `artifacts/eval/eval_checks.csv`
- `artifacts/perf/perf_smoke.json`
- `artifacts/e2e/operator_e2e_report.json`
- `artifacts/metrics/memorymaster.prom`
- `artifacts/metrics/memorymaster_metrics.json`
- `artifacts/incident_drill/<drill_id>/incident_drill_run.json`
- `artifacts/incident_drill/<drill_id>/incident_drill_evidence.md`
- `artifacts/incident_drill/<drill_id>/compaction/compaction_trace_validation.json`
- `artifacts/incident_drill/<drill_id>/signoff/drill_signoff.json`

## 10) Troubleshooting

`run-operator` appears to hang:
- set `--max-idle-seconds 60` and/or `--max-events N`
- check `--log-jsonl` output to confirm activity
- verify inbox path and JSONL line format

No results in query:
- run `run-cycle` to validate/promote recent claims
- check sensitive gating (`--allow-sensitive` requires `MEMORYMASTER_ALLOW_SENSITIVE_BYPASS=1`)
- try `--retrieval-mode hybrid`

Postgres mode:
- start local DB with `docker compose -f docker-compose.postgres.yml up -d`
- run smoke script `.\scripts\smoke_postgres.ps1`

## 11) Suggested Production Baseline

1. Run operator with checkpoint + event logs enabled.
2. Use `policy-mode cadence` and hybrid retrieval.
3. Schedule strict evaluation and E2E harness in CI.
4. Review queue daily for stale/conflicted claims.
5. Keep compaction periodic and auditable.
