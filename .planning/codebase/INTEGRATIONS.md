# INTEGRATIONS.md — External Integrations

## Model Context Protocol (MCP)
- **File:** `memorymaster/mcp_server.py`
- **Library:** `mcp>=1.2` (FastMCP)
- **Entry point:** `memorymaster-mcp`
- **Config via env vars:**
  - `MEMORYMASTER_DEFAULT_DB` — path to SQLite or DSN
  - `MEMORYMASTER_WORKSPACE` — workspace root
  - `MEMORYMASTER_DEFAULT_PROJECT_SCOPE`
  - `MEMORYMASTER_QUERY_INCLUDE_LEGACY_PROJECT`
- Tools exposed: ingest, query, query_for_context, list_claims, pin, run_cycle, add_claim_link, remove_claim_link, get_claim_links, get_linked_claims, dedup, compact, list_events, redact_claim_payload

## Qdrant Vector Database
- **File:** `memorymaster/qdrant_backend.py`
- **Library:** `httpx>=0.27` (REST API)
- **Config:** `QDRANT_URL` environment variable
- Auto-initialized in `MemoryService.__init__` if `QDRANT_URL` set
- Fire-and-forget sync on claim upsert/delete

## Gemini LLM (LLM Steward + Compact Summaries)
- **Files:** `memorymaster/llm_steward.py`, `memorymaster/jobs/compact_summaries.py`
- **Library:** `google-genai>=1.0`
- Used for: LLM-based claim stewardship, semantic summary compaction

## PostgreSQL
- **File:** `memorymaster/postgres_store.py`
- **Library:** `psycopg[binary]>=3.2`
- Schema: `memorymaster/schema_postgres.sql`
- Same interface as `SQLiteStore`

## Sentence Transformers (Local Embeddings)
- **File:** `memorymaster/embeddings.py`
- **Library:** `sentence-transformers>=3.0`
- Used for semantic vector similarity in hybrid retrieval and dedup
- Falls back to simple cosine similarity if unavailable

## Cryptography (Sensitive Claims)
- **File:** `memorymaster/security.py`
- **Library:** `cryptography>=42`
- Used for encrypting sensitive claim payloads at rest
