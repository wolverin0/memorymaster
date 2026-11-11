# MCP Server Rules

When modifying `mcp_server.py`:
- Every new tool must have auto-citation fallback (CitationInput, not dict)
- Every tool that writes claims must pass through the sensitivity filter
- Always pass `source_agent` to `svc.ingest()`
- Test with `mcp__memorymaster__ingest_claim` after changes — don't just check Python syntax
- Trust model: in `local-trusted` mode every session is admin and `source_agent` is self-declared, so operator-only gates (`MEMORYMASTER_MCP_OPERATOR_RESOLVE=1` for approving operator-only proposals and `archive_by_source(dry_run=False)`) are hygiene, not a boundary: declaring the target author's name makes a supersession same-identity, and `forget(apply=True)`/`redact_claim_payload` can retire or blank another agent's claim. The real fix, authenticated per-client principals, is not done. Do not describe these gates as security.
