# MCP Tools Reference

MemoryMaster exposes **30 MCP tools** over a FastMCP stdio server (`memorymaster-mcp`).
This reference is generated from the tool docstrings in
[`memorymaster/surfaces/mcp_server.py`](../memorymaster/surfaces/mcp_server.py) — one line per
tool, grouped by purpose. For the MCP server config block see the
[README](../README.md#mcp-server) and [`.mcp.json.example`](../.mcp.json.example).

> Every write tool passes through the sensitivity filter and auto-citation fallback, and
> tags `source_agent`. See [`.claude/rules/mcp-server.md`](../.claude/rules/mcp-server.md).

## Setup & lifecycle

| Tool | Purpose |
|------|---------|
| `init_db` | Initialize MemoryMaster database schema. |
| `run_cycle` | Run one full maintenance cycle: extract, deterministic validate, validate, decay, compact (optional). |
| `run_steward` | Run the stewardship loop and emit an audit report artifact. |
| `compact_memory` | Archive old stale/superseded/conflicted claims and trim events. |

## Ingest (write)

| Tool | Purpose |
|------|---------|
| `ingest_claim` | Ingest a claim into memory. |
| `ingest_rule` | Ingest a prescriptive rule-shaped claim (`when <trigger>, do <action> because <rationale>`). |
| `redact_claim_payload` | Redact or erase claim/citation payload non-destructively with an audit event. |
| `pin_claim` | Pin or unpin a claim by id. |

## Query & retrieval (read)

> **R1.3 Qdrant containment:** local-trusted `query_memory(retrieval_mode="qdrant")`
> reports the requested/effective modes and uses authoritative lexical fallback.
> Auto-classification follows the same rule. Prompt-context Qdrant fallback is
> disconnected, and `search_verbatim(mode="vector"|"hybrid")` reports an FTS5
> fallback instead of returning Qdrant payloads. Team MCP denies every semantic
> mode, including Qdrant and local hybrid. Qdrant sync/reconcile are CLI/index
> maintenance operations, not MCP read paths. Governed Qdrant reads are deferred
> to R2.1 ID-candidate retrieval plus SQLite/Postgres rehydration.

| Tool | Purpose |
|------|---------|
| `query_memory` | Query authoritative claims; a local-trusted Qdrant request falls back to lexical retrieval, while team semantic requests are denied. |
| `query_for_context` | Pack the most relevant claims into a token-budgeted context block. |
| `query_for_task` | Look-ahead task-aware briefing for an upcoming PRD task. |
| `query_rules` | Retrieve rule-shaped claims matching a query, in prescriptive form. |
| `query_claim_paths` | Traverse claim relationship paths from a starting claim (read-only). |
| `query_meta_decisions` | Aggregate matching decision/architecture claims across all project scopes. |
| `federated_query` | Query across ALL scopes — cross-project federation. |
| `classify_query` | Classify a query and report both the recommended mode and its containment-safe effective mode. |
| `recall_analysis` | Explain WHY each claim ranked where it did (ranking introspection). |
| `search_verbatim` | Search raw conversation memories through FTS5; vector/hybrid requests currently report an FTS5 fallback. |
| `read_active_tasks` | Read and parse the project's `active_tasks.md`. |
| `rules_export` | Export mined rule-shaped claims, filtered by confidence + status. |

## Listing & inspection

| Tool | Purpose |
|------|---------|
| `list_claims` | List claims by optional status. |
| `list_events` | List events by optional `claim_id` and `event_type`. |
| `quality_scores` | Recompute quality scores for all claims based on usage feedback. |
| `recompute_tiers` | Recompute memory tiers (core/working/peripheral) based on access patterns. |

## Knowledge graph

| Tool | Purpose |
|------|---------|
| `extract_entities` | Extract entities from a claim's text and link them to the knowledge graph. |
| `entity_stats` | Get entity knowledge-graph statistics. |
| `find_related_claims` | Find claims related to entities via knowledge-graph traversal. |

## Governance (steward review)

| Tool | Purpose |
|------|---------|
| `list_steward_proposals` | List steward proposals for the human override workflow. |
| `resolve_steward_proposal` | Approve or reject a steward proposal by `proposal_event_id` or `claim_id`. |
| `open_dashboard` | Return the local dashboard URL and optionally check reachability. |
