# Sensitivity Filter Rules

MemoryMaster ingests ambient data from LLM transcripts, dream bridge, and MCP calls. The sensitivity filter is the firewall that prevents secrets from being stored. Bypassing it — accidentally or "just this once" — is a security incident.

## Never ingest

- API keys, tokens, bearer headers, auth cookies — regardless of provider
- Passwords, PINs, OTP codes
- Private IP addresses (10.*, 172.16-31.*, 192.168.*, link-local)
- Personal home directory paths that expose usernames
- Raw code snippets longer than ~3 lines (use symbol references instead)
- Database connection strings with credentials
- Email addresses of third parties (user's own email is OK if explicitly opted in)
- Payment card data, SSN, national ID numbers

## Where the filter applies

The filter MUST run on:
- `mcp_server.py:ingest_claim` — every MCP call from any client
- `dream_bridge.py` — every auto-ingest from Claude Auto Dream
- `service.py:ingest` — every direct service call (if callers bypass MCP)
- Any new ingest path — default-deny until filter is wired in

## Never add a bypass

There is no legitimate reason to have an `allow_sensitive=True` flag on ingest. If a genuine use case arises (e.g., structured secrets metadata for lifecycle management):
1. Add a separate typed table with its own access control, NOT a flag on claims.
2. Require explicit CLI argument, never default-on.
3. Document in SECURITY section of README.

## When you find a sensitive claim in the DB

1. Do NOT paste it into a log, a commit message, or a reply to the user.
2. Use `redact_claim_payload` to replace the text with a hash + redacted marker.
3. File an internal note about the ingest path that let it through.
4. Patch the filter pattern if new.

## Testing the filter

Every change to the filter must ship with a `tests/test_sensitivity_filter.py` case that proves the new pattern is caught. Red-bar first, green-bar second. If `pytest -k sensitivity` doesn't exist or is skipped, that's a bug.

## Filter scope is INGEST only — not display

Display-time redaction is a separate layer (`redact_claim_payload`, dashboard masks). The INGEST filter is the last line of defense; do not weaken it assuming display will catch it.
