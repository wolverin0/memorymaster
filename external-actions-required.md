# External Actions Required

Items here are `BLOCKED-EXTERNAL` only when the remediation ledger explicitly marks them that way. Repository work continues independently.

| Finding | Owner/system | Required action | Evidence needed to unblock | Review date | Status |
|---|---|---|---|---|---|
| MM-OPS-01 | Operator / any Postgres deployment | Rotate the historical `mm_pw` credential and recreate deployments that used it. Keep database ports private. | Rotation record plus network probe showing the port is not externally reachable | 2026-07-31 | PENDING-INVENTORY |
| MM-SEC-01 | Team Postgres test environment | Provide `MEMORYMASTER_TEST_POSTGRES_DSN` for adversarial RLS and application-role tests. | CI/runtime output proving cross-tenant SELECT/UPDATE denial | 2026-07-31 | BLOCKED-EXTERNAL |
| MM-OPS-02 | Docker/Helm runtime | Provide Docker and, for final verification, a disposable Kubernetes target if unavailable locally. | Container health/MCP handshake and Helm readiness/network-policy evidence | 2026-08-15 | PENDING-CAPABILITY-CHECK |
| MM-SEC-02 | Qdrant runtime | Provide a disposable authenticated/TLS Qdrant target for final parity after fake-backed tests pass. | Real service policy/reconciliation test output | 2026-08-15 | PENDING-CAPABILITY-CHECK |
| MM-DATA-01 | Live MemoryMaster operator | Approve a consistent backup/restore drill before any live migration, redaction, backlog, or retention operation. | Restored backup, integrity check, counts/checksums, approval record | 2026-08-15 | BLOCKED-EXTERNAL |
| MM-CAP-01 | Host storage operator | Address/monitor the drive at 85.82% used without deleting MemoryMaster data under this goal. | Daily disk telemetry below the critical gate or approved capacity expansion | 2026-07-18 | BLOCKED-EXTERNAL |
| MM-PRIV-01 | Product owner / legal reviewer | Decide intended organizational use, jurisdictions, processor disclosures, and retention commitments before compliance claims. | Approved privacy/data-processing statement | 2026-08-31 | BLOCKED-EXTERNAL |
