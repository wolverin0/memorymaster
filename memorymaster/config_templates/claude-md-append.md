## MemoryMaster (Cross-Session Memory) — MANDATORY

You have access to the `memorymaster` MCP server. It stores facts, decisions, and constraints across ALL sessions and providers (Claude, Codex, Gemini).

### READ before assuming
Before making architectural decisions or debugging unfamiliar code, call `mcp__memorymaster__query_memory` with the relevant topic. A previous session may have already solved this.

### WRITE when you learn something non-obvious
After completing a task where you discovered something that would save future sessions time, call `mcp__memorymaster__ingest_claim`:

**Always ingest:** bug root causes, architectural decisions, environment gotchas, integration patterns, constraints ("never do X because Y")

**Never ingest:** credentials, API keys, tokens, private IPs, personal paths, code snippets, routine actions

```
mcp__memorymaster__ingest_claim({
  "text": "One-line factual description",
  "claim_type": "fact|decision|constraint",
  "subject": "entity",
  "predicate": "aspect",
  "object_value": "value",
  "scope": "project:<project-name>"
})
```

This is NOT optional. If you fixed a non-trivial bug, made an architecture decision, or discovered a gotcha — ingest it.
