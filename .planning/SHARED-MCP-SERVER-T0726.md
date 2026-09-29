<!-- doc-head: design + measured POC for ONE shared MemoryMaster MCP server for all sessions (T-0726) -->
<!-- Covers: per-client scope resolution (header, relay, URL path, roots/list), fail-closed shared-server workspace rule, placeholder rejection, process-wide model cache, mandatory allowlist, hooks, POC numbers, deploy plan, client config proposals. -->
<!-- Key terms: X-MM-Workspace, mcp_stdio_proxy relay, stateless streamable HTTP, 127.0.0.1:8766, MEMORYMASTER_MCP_WORKSPACE_ALLOWLIST (mandatory), ship T-0725+T-0726 together, fallback. -->
<!-- Read when: deploying the shared server, changing MCP scope resolution, or editing client MCP configs. -->
<!-- Status: POC on branch poc/t0726-shared-mcp (T-0725 0c959ed + T-0726 9d29abb + verifier fixes); NOT installed; ship as one unit; client configs are operator-owned proposals. -->
<!-- /doc-head -->

# One shared MemoryMaster MCP server (T-0726)

## Recommendation

Run **one** streamable-HTTP MemoryMaster on `127.0.0.1:8766` and have every
client launch a **thin stdio relay** (`python -m memorymaster.surfaces.mcp_stdio_proxy`)
instead of the full server. The relay sends each client's working directory as
the `X-MM-Workspace` header; the server resolves the project scope from that
header per request, not from its own cwd. The header is also the contract for
any client that later talks HTTP directly.

Why the relay rather than direct HTTP everywhere: it is the only option that
works unchanged in all four clients, needs **no per-repository config**, and
keeps today's cwd semantics exactly. The cost is one ~18 MB stdlib process per
session instead of ~2 GB.

## Measured (disposable DB, 2026-09-29, `artifacts/t0726/poc.py`)

| | per session | shared backend | 20 sessions |
|---|---|---|---|
| today: stdio server per pane | 1,952 MB private at start | — | ~39 GB |
| T-0725 only (thread cap) | 472 MB start / 747 MB after 3 hybrid queries | — | ~9.4–15 GB |
| **shared server + relays** | **17.7 MB** (relay) | 472.5 MB ready / 735.4 MB after both clients | **~1.1 GB** |

Isolation, two clients in different repos (`alpha`, `beta`) run concurrently
against one server whose own cwd is a third directory: each ingest landed in
`project:alpha` / `project:beta`; each client's lexical **and** hybrid recall
returned only its own fact. After the verifier fixes (rerun 2026-09-29): a
header-less call and a `${CLAUDE_PROJECT_DIR}` header are both **refused** with a
clear tool error; the model loads **once** for the whole server (1 load for 6
hybrid calls from 2 clients), so the first hybrid call pays 2.9 s and later ones
take 0.06 s; both clients finished in 4.4 s (6.8 s before); server 472 MB ready,
643 MB after both clients (735 MB before the cache).

## The three scope mechanisms today, and the change

- `workspace` — every tool takes `workspace="."`. `_resolve_workspace` returns it
  and `_project_scope` resolves it with `Path(".").resolve()`, i.e. **the server
  process's cwd**, then `project:<slug of that directory>`. In a stdio server the
  client launches it from the project directory, so this works; in a shared
  server every repository collapses into the server's slug.
- `MEMORYMASTER_MCP_WORKSPACE_ALLOWLIST` (`mcp_path_policy`) — validates the
  resolved workspace; silent when unset. The header path goes through the same
  validation. **On the shared server it is mandatory**: any holder of the bearer
  token chooses a workspace through the header or argument, so without the
  allowlist a token holder can read/write any project scope. Set it to the roots
  it serves (the `Py Apps` tree and `C:/Users/pauol/orca/workspaces`).
- Default recall scopes — `_effective_scope_allowlist` builds
  `[project:<slug>, global, …enclosing-project and user scopes]` from the same
  workspace, so fixing the workspace fixes recall scope too (verified in the POC).

**Change (commit 9d29abb):** in `_authorized_tool_callable` — the single wrapper
every tool passes through — when a tool received the default workspace (`""` or
`"."`) and the HTTP request carries `X-MM-Workspace`, that value replaces it
before authorization. An explicit `workspace` argument still wins. In team mode
the existing check still compares it with the authenticated workspace and
refuses a mismatch (fail-closed); Hermes (team mode on 8765) is unaffected.

### Verifier fixes (T-0726 return)

- **Fail-closed on the shared server.** Over HTTP in `local-trusted` mode a tool
  whose workspace is still `""`/`"."` (no header, no argument) or is relative is
  refused, for reads and writes alike — the server's cwd is never a project.
  stdio keeps the `"."` default (the client launched it from the project).
  Team mode keeps its own authenticated-workspace check.
- **Unexpanded placeholders refused** on every transport: `${…}`, `{env:…}`,
  `$VAR…`, `%VAR%` (they used to become `project:claude_project_dir` /
  `project:env-pwd`).
- **Model cache.** `recall/embeddings._load_transformer` keeps one loaded
  SentenceTransformer per model per process (lock-guarded), so per-request
  services reuse it.

Tests: `tests/test_shared_mcp_client_workspace.py` (red on 9d29abb: 13 failures;
green after).

## Options compared (AC1)

| option | how scope reaches the server | Claude Code 2.1.284 | Codex 0.159 (+Orca daemon) | OpenCode 1.18.33 | agy |
|---|---|---|---|---|---|
| **stdio relay** (recommended) | relay's cwd / `CLAUDE_PROJECT_DIR` → header | yes: stdio, launched from the project dir | yes: each thread launches stdio servers from its cwd | yes: `type: local` | yes: stdio |
| header per repo | static header in per-repo config | yes: `.mcp.json` `headers` with `${VAR}` expansion | **no clean way**: only a global `config.toml`; `env_http_headers` exists (found in the 0.159 binary) but the Orca app-server is one daemon for all threads with one env, so the value cannot differ per repo | yes: project `opencode.json`, `headers` | unverified: `serverUrl`; header support and Streamable-HTTP compatibility reported unreliable |
| path per project in URL (`/mcp/w/<slug>`) | route prefix → scope | yes, per-repo `.mcp.json` | same limit as header | yes | likely (URL only), unverified |
| MCP `roots/list` | server asks client for its roots | answers roots over HTTP (Fleet) | **no** over HTTP (openai/codex#37903) | answers roots over HTTP (Fleet) | answers roots over HTTP (Fleet) |

`roots/list` is the spec-native answer but needs **stateful** sessions: the
server must send a request back to the client, and the current HTTP server runs
`stateless_http=True` (no session stream). It also ties scope to per-client
features we cannot verify for three of four clients. URL-per-project is
equivalent to the header for clients that cannot set headers but loses the
enclosing-project scope logic (a slug is not a path) and still needs per-repo
config. Neither is implemented; both can be added later behind the same
workspace substitution. The 2026-07-28 MCP spec revision also deprecates
roots and sessions, so header/argument (what the relay sends) is the durable
contract.

## Hooks (AC2)

The hooks (`memorymaster-recall.py`, `-session-start`, `-auto-ingest`,
`-precompact`, Dreaming capture, …) **do not use MCP**. Each is a fresh Python
process that imports MemoryMaster in-process and derives scope from its own cwd
and transcript. The shared server changes nothing for them: they keep working
if it is down, and they keep paying their cold start (T-0594: recall hook median
5.4 s). A later, separate step could let hooks call the warm shared server over
HTTP to remove that cold start; it is out of scope here.

## Findings to fix before rollout

1. ~~Model reloaded on every hybrid request~~ — fixed (process-wide cache, see
   Verifier fixes).
2. **Per-session telemetry** is keyed by `(db, principal, tenant)` in
   `_TELEMETRY_SESSION_IDS`, so all relays share one telemetry session. Add the
   declared workspace to the key.
3. The HTTP entrypoint lacked the Windows native-ML pre-import and the thread
   cap; both added in 9d29abb (the POC's first hang was an undrained stderr pipe
   in the harness, not the server, but a lazy torch import inside the event loop
   is the known Windows stall and the pre-import is cheap insurance).

## Deploy plan (AC4) — proposal; install and client configs are operator decisions

1. **Ship T-0725 and T-0726 together.** On 0c959ed alone `mcp_http.main()` has
   no thread cap and no native-ML pre-import (both come in 9d29abb), so an HTTP
   server built from T-0725 only would still run 32-thread BLAS pools. **Build +
   install** one wheel containing both (+ the verifier fixes) into both
   runtimes (client Python 3.12 and `graph-profile-20260813`), same procedure and
   rollback wheel as 4.9.0.
2. **Service** `MemoryMaster-MCP-Shared` (new scheduled task, separate from
   `MemoryMaster-MCP-HTTP-Hermes` on the LAN IP/team mode): at logon, restart on
   failure every 1 min, run hidden:
   `pythonw -m memorymaster.surfaces.mcp_http --host 127.0.0.1 --port 8766`
   with `MEMORYMASTER_MCP_AUTH_MODE=local-trusted`, `MEMORYMASTER_DEFAULT_DB`,
   `MEMORYMASTER_MCP_WORKSPACE_ALLOWLIST` (the served roots — **mandatory**; the code does not enforce it, so the
   task definition must never omit it) and
   `MEMORYMASTER_MCP_HTTP_TOKEN` (user env; never in a command line).
3. **Health**: `/healthz` (liveness) and `/readyz` (DB) — ask infra for a Kuma
   monitor on `127.0.0.1:8766/readyz`.
4. **Fallback if the server is down**: the relay answers every request with a
   JSON-RPC error naming the URL (fast, visible); hooks are unaffected. Optional
   hardening: `MEMORYMASTER_PROXY_FALLBACK=local` so a relay that cannot reach the
   server at startup runs the full stdio server instead (today's behaviour and
   cost) — not implemented yet.
5. **Pilot** one pane (the MemoryMaster pane), then migrate the rest and measure
   the sum of private memory of MemoryMaster MCP processes before/after (the
   T-0725 AC4 baseline: 20 processes, 36.9 GB on 2026-09-29 17:10).

### Client config proposal (do not apply without the operator)

Every client keeps a stdio entry; only the command changes. `MEMORYMASTER_MCP_HTTP_TOKEN`
is read from the user environment, never written into these files.

Claude Code (`.mcp.json` / `~/.claude.json`):
```json
"memorymaster": {"command": "C:\\Users\\pauol\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
  "args": ["-I", "-m", "memorymaster.surfaces.mcp_stdio_proxy"],
  "env": {"MEMORYMASTER_SHARED_MCP_URL": "http://127.0.0.1:8766/mcp"}}
```
Codex (`~/.codex/config.toml` **and** the Orca runtime copy):
```toml
[mcp_servers.memorymaster]
command = "C:\\Users\\pauol\\AppData\\Local\\Programs\\Python\\Python312\\python.exe"
args = ["-I", "-m", "memorymaster.surfaces.mcp_stdio_proxy"]
env = { MEMORYMASTER_SHARED_MCP_URL = "http://127.0.0.1:8766/mcp" }
```
OpenCode (`opencode.jsonc`): `"memorymaster": {"type": "local", "command": [<python>, "-I", "-m", "memorymaster.surfaces.mcp_stdio_proxy"], "environment": {"MEMORYMASTER_SHARED_MCP_URL": "http://127.0.0.1:8766/mcp"}}`
agy (`mcp_config.json`): same stdio command and env.

## Verification on the branch

`tests/test_shared_mcp_client_workspace.py` (24 tests: header substitution,
explicit argument wins, stdio default unchanged, reading the SDK request context,
shared-server fail-closed for ingest and query, absolute header accepted,
placeholders refused via header and argument, model loaded once, relay
header/bearer, notification silence, unreachable and bad-URL errors, workspace
priority) plus the MCP startup, authorization/tenant-scope boundary,
HTTP entrypoint, architecture and public-demo suites: 95 + 80 passed; Ruff clean.
Evidence: `artifacts/t0726/poc.py`, `poc-result.json` (ignored directory).
