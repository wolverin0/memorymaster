# Rollback Plan

## Before release

Revert the Phase 4 convergence commit first, then revert R4.4 through R4.1 in
reverse order. If a phase-level rollback is required, continue with Phase 3
and Phase 2 convergence/package commits in reverse dependency order.

## Data and schema safety

- Do not edit or delete immutable migrations 0013–0016.
- Do not downgrade a database in place. Restore a verified compatible backup
  or ship a reviewed forward repair.
- Disable semantic mode and use authoritative lexical retrieval if Qdrant
  evidence or reconciliation fails.
- Disable Team/Postgres profiles if identity/RLS evidence is absent.
- Return capture to quiet/no-capture defaults; never restore unlimited
  per-stop or synthetic production capture.

## Runtime rollback

1. Keep the last approved immutable image and artifact hashes available.
2. Stop canary promotion on failed health, readiness, MCP handshake,
   migration, tenant/scope, integrity, or telemetry gates.
3. Redeploy the last approved image without rolling database migrations back.
4. Verify `/healthz`, `/readyz`, MCP initialize/tools-list, DB integrity/FKs,
   tenant/scope denials, Qdrant fallback, and alert delivery.
5. Restore product data only from a separately verified backup and only with
   explicit operator approval.
