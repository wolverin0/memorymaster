# Draft PR — Phase 3 performance and operational readiness

## Summary

- Persist embedding fingerprints and replayable Qdrant reconciliation cursors.
- Add generation-aware query statistics, durable shared budgets, event indexes,
  finite result contracts, and deterministic performance smoke.
- Make setup profiles truthful from a clean wheel.
- Separate stdio MCP, authenticated HTTP MCP, and dashboard deployment profiles
  with real readiness and bounded resources.
- Add retry leases, encrypted recovery drills, persistent operational health,
  attributable audit envelopes, and read-only privacy planning.

## Verification

- Phase 3 matrix: 193 passed.
- Full non-ML: 4,042 passed with 10 corrected failures; exact correction 19
  passed. A second full run was intentionally not performed.
- ML: 93 passed plus exact two-test correction.
- Quick SLO: PASS at 20.65 query ops/s and 0.0547s p95.
- Clean-wheel minimal setup, encrypted SQLite restore, Compose, dashboard/MCP
  readiness, and stdio/authenticated HTTP MCP handshakes: PASS.
- Project Ruff and diff check: PASS.

## Required external review before release

- Resolve every applicable BLOCKED-EXTERNAL row in
  `external-actions-required.md`, especially Postgres/Qdrant/Kubernetes,
  off-device recovery, history/image scans, telemetry, and privacy/legal review.
- Build and scan approved immutable release artifacts; the local image digest in
  this delta is development evidence only.
- No push, merge, publish, deploy, credential rotation, or live-data operation
  is part of this PR draft.
