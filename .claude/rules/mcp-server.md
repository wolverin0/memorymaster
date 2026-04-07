# MCP Server Rules

When modifying `mcp_server.py`:
- Every new tool must have auto-citation fallback (CitationInput, not dict)
- Every tool that writes claims must pass through the sensitivity filter
- Always pass `source_agent` to `svc.ingest()`
- Test with `mcp__memorymaster__ingest_claim` after changes — don't just check Python syntax
