# MemoryMaster ↔ Hermes-Agent Integration Brief

**Audience:** hermes-agent (an LLM-driven harness running on the Ubuntu VM).
**Author:** Claude Code session running on the Windows PC.
**Date:** 2026-05-18.
**Goal:** Wire memorymaster into hermes the same way it was wired into openclaw
(now paused), or — if hermes can do better than scp-based DB sync — pick a
tighter integration. Then send back a verification artifact proving the
connection works.

---

## Why you (hermes) are reading this

The Windows PC runs **memorymaster** — a memory-reliability system for AI agents
(claims with lifecycle/status/tier, citations, conflict detection, steward
governance, MCP server). The previous integration was a 15-min cron job on
openclaw (`scripts/openclaw-sync.sh`) that scp'd the Windows DB to /tmp, ran
`memorymaster merge-db --source <other>` in both directions, and scp'd the
merged DB back.

openclaw is paused. You're the active harness on the Ubuntu VM now. We want
the same shape — bidirectional memory sync between you and the Windows
memorymaster — OR a better integration if your runtime supports it.

---

## What memorymaster looks like from the outside

| Surface | How to reach it |
|---|---|
| GitHub source | https://github.com/wolverin0/memorymaster |
| Local on Windows | `G:\_OneDrive\OneDrive\Desktop\Py Apps\memorymaster\memorymaster.db` |
| Python package | `pip install "memorymaster[mcp,qdrant,security]"` (Python 3.10+) |
| CLI | `python -m memorymaster --help` (or `memorymaster --help` if installed as script) |
| MCP server | FastMCP stdio: `python -m memorymaster.mcp_server` — 21 tools (query, ingest, run_cycle, etc.) |
| Dashboard (HTTP) | `python -m memorymaster --db <DB> run-dashboard --port 8765` |
| Webhook | Outbound only — fires `MEMORYMASTER_WEBHOOK_URL` on claim events |

**Recent shipped versions you should know about:**

- **v3.18.0** — per-question-type retrieval profiles + `claude_cli` bench judge
- **v3.19.0** (Phase 0 hardening) — LLM budget caps, dashboard auth (viewer/operator roles, CSRF, bind-safety), webhook HMAC signing + replay protection, MCP db/workspace allowlist
- **v3.20.0-S1** — versioned migrations framework (`memorymaster/migrations/`, `MigrationRunner`, `schema_versions` table, drift detection)

Full env-var reference in `CHANGELOG.md`. ROADMAP and remaining backlog in
`docs/ROADMAP.md` + `docs/v320-backlog.md`.

---

## What we know about you (hermes-agent) — limited

We have:

- You are a "thin harness" / few-thousand-LOC router, same architectural
  class as openclaw (per MemoryMaster claim `mm-37f8`).
- You run on the same Ubuntu VM as the (now-paused) openclaw and the
  openclaw-gateway Telegram-bot poller.
- You and openclaw-gateway were once conflated in our memory — they're
  separate processes.

We don't have:

- Your source location (repo? local-only?)
- Your runtime model (Python? Node? Rust?)
- Your memory model — do you persist anything, or are you stateless per
  invocation?
- Whether you can act as an MCP client.
- Whether you can call out via HTTP.

**Please self-report these in your verification artifact (below).**

---

## The openclaw template (for reference, copy or improve)

Existing script: `scripts/openclaw-sync.sh` on the Ubuntu VM.

```bash
#!/bin/bash
# Runs as cron every 15 min on the Ubuntu VM.
WINDOWS_HOST="${WINDOWS_HOST:-windows-pc}"           # SSH alias
WINDOWS_DB_PATH="${WINDOWS_DB_PATH:-/g/_OneDrive/OneDrive/Desktop/Py Apps/memorymaster/memorymaster.db}"
LOCAL_DB="${LOCAL_DB:-/opt/memorymaster/memorymaster.db}"
MM="${MEMORYMASTER_BIN:-memorymaster}"

# 1. Pull Windows DB to /tmp via scp
scp -q "$WINDOWS_HOST:$WINDOWS_DB_PATH" /tmp/windows.db

# 2. Merge Windows → Local (new claims from Windows land in your local DB)
$MM --db "$LOCAL_DB" merge-db --source /tmp/windows.db

# 3. Merge Local → Windows-copy (your local claims land in the Windows copy)
$MM --db /tmp/windows.db merge-db --source "$LOCAL_DB"

# 4. scp the merged copy back to Windows
scp -q /tmp/windows.db "$WINDOWS_HOST:$WINDOWS_DB_PATH"
```

The `merge-db` command is idempotent (dedup via `idempotency_key` + sha256
text-hash), preserves both sides' claims, and resolves conflicts by higher
`confidence` (newer `updated_at` on tie).

---

## Pick one of three integration paths

| Path | Best when | Cost | Latency |
|---|---|---|---|
| **A. DB-sync clone** | You have / can install your own memorymaster + local DB | ~10 min | 15-min cron |
| **B. MCP client** | You're an LLM router and can speak MCP stdio | ~1-2 h | Live |
| **C. HTTP adapter** | You can't speak MCP but can hit REST + bearer token | ~3-4 h | Live |

### Path A — clone the openclaw script

```bash
# 1. Install memorymaster on your side
pip install "memorymaster[mcp,qdrant,security]"

# 2. Initialise your local DB
mkdir -p /opt/memorymaster
memorymaster --db /opt/memorymaster/memorymaster.db init-db

# 3. Copy the template, point env vars at the Windows host
sudo cp /opt/memorymaster/scripts/openclaw-sync.sh /opt/memorymaster/scripts/hermes-sync.sh
# Edit env defaults inside or set via cron line.

# 4. Cron entry (every 15 min)
(crontab -l 2>/dev/null; echo "*/15 * * * * /opt/memorymaster/scripts/hermes-sync.sh >> /var/log/memorymaster-sync.log 2>&1") | crontab -

# 5. Run once manually as a smoke test
bash /opt/memorymaster/scripts/hermes-sync.sh
```

**Required:** SSH from VM to Windows PC must be passwordless (or scp will
prompt). Confirm before running: `ssh windows-pc echo ok`.

### Path B — MCP client wiring

Memorymaster ships a FastMCP stdio server. If you can speak MCP:

```bash
# On the Ubuntu VM (assumes pip install above already done)
python -m memorymaster.mcp_server
# Speaks MCP on stdin/stdout. Wire it into your client config.

# Or via ssh from the VM hitting the Windows memorymaster instance:
ssh windows-pc 'python -m memorymaster.mcp_server'
```

Key MCP tools (full list via the MCP `list_tools` call):

- `query_memory` / `query_for_context` / `query_for_task` — read claims
- `ingest_claim` — write a claim
- `run_cycle` — trigger steward validation
- `list_claims`, `list_events`, `pin_claim`, `redact_claim_payload`

**Security note:** v3.19.0-H4 added an MCP path allowlist. If you'll be
supplying `db` / `workspace` paths from the caller side, set
`MEMORYMASTER_MCP_DB_ALLOWLIST` on the server side or use
`MEMORYMASTER_MCP_ADMIN_MODE=1`.

### Path C — HTTP adapter

There isn't a first-class REST API yet, but the dashboard exposes JSON
endpoints behind v3.19.0-H2 bearer-token auth:

```bash
# On Windows, start dashboard with auth
MEMORYMASTER_DASHBOARD_TOKEN_OPERATOR=<secret> \
  python -m memorymaster --db <DB> run-dashboard --host 0.0.0.0 --port 8765

# On Ubuntu VM, query it
curl -H "Authorization: Bearer <secret>" \
  http://windows-pc:8765/api/claims?limit=10

# Operator-only routes (POST + mutating GETs like /api/operator/stream)
# return 403 with a viewer token.
```

For inbound webhook notifications (Windows → VM), set on Windows side:

```bash
MEMORYMASTER_WEBHOOK_URL=http://your-vm-host:PORT/webhook
MEMORYMASTER_WEBHOOK_SECRET=<shared-secret>
```

Your receiver verifies with `memorymaster.webhook.verify_webhook_signature`.

---

## Verification ritual — prove the connection works

After picking and executing a path, emit a JSON artifact with the shape
below. Send it back via **any one** of these channels (whichever is
easiest for you):

### Return channel options (in order of preference)

1. **Best — scp back to a known Windows path:**
   ```bash
   scp /tmp/hermes-integration-proof.json \
     windows-pc:/g/_OneDrive/OneDrive/Desktop/Py\ Apps/memorymaster/handoffs/hermes-proof.json
   ```
2. **GitHub issue** (requires `gh` + auth on the VM):
   ```bash
   gh issue create --repo wolverin0/memorymaster \
     --title "hermes-agent integration proof" \
     --body-file /tmp/hermes-integration-proof.json
   ```
3. **HMAC-signed webhook** to memorymaster (if Windows runs a receiver
   you've coordinated with the operator):
   ```bash
   ts=$(($(date +%s)*1000))
   body=$(cat /tmp/hermes-integration-proof.json)
   sig="sha256=$(printf '%s.%s' "$ts" "$body" | openssl dgst -sha256 -hmac "$MEMORYMASTER_WEBHOOK_SECRET" | awk '{print $2}')"
   curl -X POST http://windows-pc:RECEIVER_PORT/hermes-proof \
     -H "Content-Type: application/json" \
     -H "X-MemoryMaster-Timestamp: $ts" \
     -H "X-MemoryMaster-Signature: $sig" \
     --data "$body"
   ```
4. **Fallback** — print the JSON to stdout and the human operator pastes
   it back into the Claude Code session.

### JSON artifact shape (please follow exactly)

```json
{
  "integration": "memorymaster <-> hermes-agent",
  "timestamp_utc": "2026-05-18T19:42:13Z",
  "hermes_self_report": {
    "version": "<your version string or 'unknown'>",
    "runtime": "<python|node|rust|other>",
    "source_location": "<git url, local path, or 'private'>",
    "has_own_memory_db": true,
    "speaks_mcp": false,
    "can_make_http_calls": true,
    "can_be_http_server": false,
    "ssh_to_windows_works": true
  },
  "chosen_path": "A",
  "commands_run": [
    "pip install memorymaster",
    "memorymaster --db /opt/memorymaster/memorymaster.db init-db",
    "bash /opt/memorymaster/scripts/hermes-sync.sh"
  ],
  "sync_test": {
    "first_sync_exit_code": 0,
    "claims_pulled_from_windows": 12345,
    "claims_pushed_to_windows": 7,
    "merge_db_log_tail": "...last 5 lines of /var/log/memorymaster-sync.log..."
  },
  "round_trip_proof": {
    "ingested_test_claim_text": "hermes-agent integration smoke 2026-05-18",
    "ingested_test_claim_id": 99999,
    "verifiable_from_windows_via": "memorymaster --db memorymaster.db list-claims --limit 5"
  },
  "blockers": [],
  "notes": "<anything you couldn't do, anything memorymaster's side needs to change>"
}
```

If you picked path B (MCP) or C (HTTP) instead of A, fill the
`commands_run` and `sync_test` blocks with whatever's relevant for that
path (omit fields that don't apply).

---

## Final ask

Run the smallest viable test FIRST (path A is the easiest if you can ssh
to windows-pc). Send back the JSON. Don't try to do everything in one
shot — a single proven round-trip is worth more than a 90%-built
integration we can't verify.

If anything in this brief is wrong, contradictory, or assumes capabilities
you don't have, say so in the `blockers` or `notes` fields and stop.
We'll iterate from there.
