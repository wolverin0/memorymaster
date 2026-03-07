# Integration Playbooks (D5)

This document provides practical integration patterns for Codex, Claude Desktop, and generic MCP clients.

## 1) Codex + MemoryMaster MCP

### Setup
```powershell
pip install -e ".[mcp]"
memorymaster-mcp
```

Codex config example:
```json
{
  "mcpServers": {
    "memorymaster": {
      "command": "memorymaster-mcp"
    }
  }
}
```

### Recommended operational flow
1. Start MCP server with stable DB path.
2. Ensure `init_db` is called once per environment.
3. Use `ingest_claim` with citations and idempotency key for retry-safe writes.
4. Run `run_cycle` periodically (or via operator loop) to maintain status quality.
5. Query memory with explicit retrieval mode and sensitivity controls.
6. Triage steward proposals with `list_steward_proposals` and `resolve_steward_proposal`.

## 2) Claude Desktop + MemoryMaster MCP

### Setup
1. Install MemoryMaster MCP extras.
2. Configure Claude Desktop MCP server entry to launch `memorymaster-mcp`.
3. Validate tool reachability with `init_db` and `list_claims`.

### Recommended guardrails
- Require at least one citation source on ingest.
- Use deterministic runbooks for conflict/stale review.
- Avoid automatic destructive fixes; run reconciliation in report mode first.

## 3) Generic MCP Client

### Minimal capability contract
- required tools: `init_db`, `ingest_claim`, `query_memory`, `run_cycle`, `list_events`
- optional tools: `pin_claim`, `compact_memory`, `open_dashboard`, `list_steward_proposals`, `resolve_steward_proposal`

### Integration checklist
1. Set a dedicated DB namespace/path per environment.
2. Send structured citations (`source|locator|excerpt`) for all writes.
3. Attach idempotency keys to ingestion requests from retrying clients.
4. Collect operator/eval/perf artifacts for auditability.
5. Run periodic reconciliation and alert on non-zero critical findings.
6. Route proposal decisions through explicit approve/reject actions, not silent auto-apply.

## 4) Troubleshooting Patterns

- No memory retrieved:
  - run `run_cycle`
  - check `allow_sensitive` gates
  - verify query text and retrieval mode

- Duplicate ingests:
  - verify caller sends stable idempotency key
  - check claim uniqueness behavior for tuple predicates

- Integrity findings appear:
  - run `reconcile_integrity(fix=False)` and inspect summary
  - only then run `fix=True` for orphan/hash-chain repairs
