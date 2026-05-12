# MCP Tool Compliance Audit

Scope: `memorymaster/mcp_server.py` on branch `experiment/T19-mcp-tool-audit`.

Rules checked:

- Tools that accept citations must normalize to `CitationInput`, not raw `dict`.
- Tools that call `svc.ingest()` must pass `source_agent`.
- Tools with claim text ingest paths must route through the sensitivity filter.

Audit notes:

- `ingest_claim` is the only MCP tool that calls `svc.ingest()` (`memorymaster/mcp_server.py:434`).
- Citation input is exposed as `sources_json` and parsed into `CitationInput` objects by `_parse_sources_json()` (`memorymaster/mcp_server.py:180`) before `svc.ingest()` receives it.
- `ingest_claim` rejects sensitive claim text through `_sensitive_input_error()` (`memorymaster/mcp_server.py:420`) before calling `svc.ingest()`. The service layer also sanitizes claim text, object value, and citation excerpts.
- Some tools mutate existing claim rows or claim-adjacent data, but do not create new claim text.

## Tool Inventory

| Tool name | Accepts citations? | Uses CitationInput? | Accepts source_agent? | Forwards it? | Writes claims? | Calls filter? |
|---|---:|---:|---:|---:|---:|---:|
| `init_db` | No | N/A | No | N/A | No | N/A |
| `ingest_claim` | Yes, via `sources_json` | Yes, `_parse_sources_json()` returns `list[CitationInput]`; fallback uses `CitationInput` | Yes | Yes, as `source_agent=effective_source` | Yes, creates claim text through `svc.ingest()` | Yes, `_sensitive_input_error(request.text)` before ingest; service also sanitizes payload |
| `run_cycle` | No | N/A | No | N/A | Yes, lifecycle/status maintenance only | N/A, no text ingest path |
| `run_steward` | No | N/A | No | N/A | Conditional, steward proposals/status updates when `apply=True` | N/A, no text ingest path |
| `classify_query` | No | N/A | No | N/A | No | N/A |
| `query_memory` | No | N/A | No | N/A | No | N/A |
| `query_for_context` | No | N/A | No | N/A | No | N/A |
| `query_for_task` | No | N/A | No | N/A | No | N/A |
| `read_active_tasks` | No | N/A | No | N/A | No | N/A |
| `list_claims` | No | N/A | No | N/A | No | N/A |
| `redact_claim_payload` | No | N/A | No | N/A | Yes, redacts/erases existing claim payload | N/A, redaction path rather than ingest path |
| `pin_claim` | No | N/A | No | N/A | Yes, pin metadata only | N/A, no text ingest path |
| `compact_memory` | No | N/A | No | N/A | Yes, archives existing claims | N/A, no text ingest path |
| `list_events` | No | N/A | No | N/A | No | N/A |
| `search_verbatim` | No | N/A | No | N/A | No | N/A |
| `open_dashboard` | No | N/A | No | N/A | No | N/A |
| `list_steward_proposals` | No | N/A | No | N/A | No | N/A |
| `resolve_steward_proposal` | No | N/A | No | N/A | Conditional, applies steward changes to existing claims when approved | N/A, no text ingest path |
| `extract_entities` | No | N/A | No | N/A | No, writes entity graph links only | N/A, no claim text ingest path |
| `entity_stats` | No | N/A | No | N/A | No | N/A |
| `find_related_claims` | No | N/A | No | N/A | No | N/A |
| `quality_scores` | No | N/A | No | N/A | Yes, recomputes claim quality metadata | N/A, no text ingest path |
| `recompute_tiers` | No | N/A | No | N/A | Yes, recomputes claim tier metadata | N/A, no text ingest path |
| `query_meta_decisions` | No | N/A | No | N/A | No | N/A |
| `federated_query` | No | N/A | No | N/A | No | N/A |

## Violations

| Tool name | Violation category | `mcp_server.py` line | Suggested fix |
|---|---|---:|---|
| None | None found | N/A | N/A |

## Compliance Summary

- Total MCP tools audited: 25
- Fully compliant tools: 25
- Tools accepting citations: 1
- CitationInput violations: 0
- Tools accepting `source_agent`: 1
- `source_agent` forwarding violations: 0
- Tools with claim text ingest paths: 1
- Sensitivity-filter violations on claim text ingest paths: 0

## Follow-Up Track Recommendations

No required violation fix tracks were found in this audit.

Optional hardening track:

- Add regression tests that assert `ingest_claim` continues to parse `sources_json` into `CitationInput`, forwards `source_agent`, and blocks sensitive text before `svc.ingest()`.
