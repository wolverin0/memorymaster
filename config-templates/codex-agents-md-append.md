## MemoryMaster (Cross-Session Memory)

You have access to the `memorymaster` MCP server with 21 tools for persistent memory across sessions and providers.

### MUST DO — Query before assumptions
Before making architectural decisions, call `query_memory` or `query_for_context` with the relevant topic. Another session (Claude, Codex, or Gemini) may have already solved this or made a decision.

### MUST DO — Ingest non-obvious learnings
When you discover something that would save future sessions time, call `ingest_claim`:

**Always ingest:**
- Decisions made ("we chose X over Y because Z")
- Bug root causes ("the 403 was caused by missing RLS policy")
- Environment gotchas ("Docker build crashes the VM, use docker pull instead")
- Integration patterns ("MercadoPago webhooks need /api/mp/webhook endpoint")
- Architecture constraints ("max 800 lines per file, immutable patterns only")

**Never ingest:**
- Credentials, API keys, tokens, passwords
- Private IPs or personal paths
- Code snippets (the code itself is in git)
- Ephemeral task state ("currently working on X")

### Ingest format
```
ingest_claim({
  "text": "Clear one-line description of the fact/decision",
  "claim_type": "fact" | "decision" | "constraint",
  "subject": "the entity this is about",
  "predicate": "what aspect",
  "object_value": "the value",
  "scope": "project:<project-name>",
  "source": "codex-session"
})
```
