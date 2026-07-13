# MemoryMaster — Agent Install Prompt

Copy the appropriate block below and paste it as a prompt to your coding agent.
The agent will run the installer, read the machine-readable result, and report
what was wired, what was reused (brownfield), and what degraded.

R1.3 safety note: Qdrant is an optional maintenance index. Installing its
extra or starting Docker does not enable claim, context-fallback, or verbatim
payload retrieval. Local-trusted Qdrant claim requests use lexical fallback;
team semantic MCP requests are denied. Sync/reconcile remain available.

---

## Claude Code variant

```
Install MemoryMaster in this environment. Follow these steps exactly.

1. Install the package with the full-stack extras:

   pip install "memorymaster[mcp,security,qdrant,embeddings]"

2. Run the detect-first, non-interactive installer and capture the JSON output:

   memorymaster-setup --yes --full-stack --json

   The installer prints human-readable progress to stderr and emits a single
   JSON document to stdout. Parse the JSON and report back to me:

   - detected: which tools were found (Docker, Qdrant, Ollama, Claude Code,
     Codex, existing hooks, MCP registration state).
   - planned: the list of [will-do / skip-present / cant-missing] lines the
     installer printed — paste them verbatim so I can see the plan.
   - applied: what was actually wired (hooks, mcp_claude, cron, full_stack, etc.).
   - degraded: true/false. If true, explain the reason from the JSON
     (typically: Docker not found → SQLite-only mode, Qdrant index maintenance
     + local LLM auto-ingest are off).
   - verify.status: PASS, PARTIAL, or FAIL and the detail string.

3. Run the verify round-trip to confirm hooks and DB are functional:

   memorymaster-setup --verify-only

   Show me the output.

4. Tell me to restart my Claude Code session. MCP and hooks take effect only
   after a full session restart.

Notes for you (the agent):
- The JSON payload shape is:
  {"detected": {...}, "planned": [...], "applied": {...}, "verify": {"status": "...", "detail": "...", "mcp_note": "..."}, "degraded": bool}
- If --full-stack brings up Docker services it may take up to 2 minutes; wait
  for the process to exit before reading stdout.
- If degraded is true the install still succeeded (exit 0). Report the
  degraded message from applied.full_stack.message so I know what to do next.
- The MCP server is registered as memorymaster.surfaces.mcp_server (not the
  deprecated memorymaster.mcp_server path). Do not edit the MCP entry manually.
- Hooks installed: UserPromptSubmit (recall + classify), PostToolUse
  (validate-wiki on Edit/Write), SessionStart (context injection on
  startup/resume), Stop (auto-ingest after each response), PreCompact (save
  before context compaction). All are idempotent; re-running the installer is
  safe.
```

---

## Codex variant

```
Install MemoryMaster in this environment. Follow these steps exactly.

1. Install the package with the full-stack extras:

   pip install "memorymaster[mcp,security,qdrant,embeddings]"

2. Run the detect-first, non-interactive installer and capture the JSON output:

   memorymaster-setup --yes --full-stack --codex --json

   The --codex flag ensures ~/.codex/config.toml is updated with the MCP
   server entry (Codex reads MCP config from there, not from ~/.claude.json).
   Parse the JSON and report back to me:

   - detected: which tools were found (Docker, Qdrant, Ollama, Codex
     presence, MCP registration state).
   - planned: paste the [will-do / skip-present / cant-missing] lines verbatim.
   - applied: what was actually wired (mcp_codex, cron, full_stack, etc.).
   - degraded: true/false and the reason if true.
   - verify.status: PASS, PARTIAL, or FAIL and the detail string.

3. Run the verify round-trip to confirm DB and core service are functional:

   memorymaster-setup --verify-only

   Show me the output.

4. Session-end memory distillation (no native Stop hook in Codex):
   Codex has no Stop hook equivalent, so learnings from each session are NOT
   distilled automatically. To enable this, wire the reference script at the
   end of each Codex session (or as a notify/exit hook in your Codex config):

   python scripts/agent_session_end_ingest.py \
     --db <path-to>/memorymaster.db \
     --transcript <rollout.jsonl> \
     --source-agent codex-session \
     --cwd <project-root>

   This distills up to 3 learnings per session, sets source_agent, and routes
   through service.ingest (never raw-INSERTs). The script path is relative to
   the MemoryMaster repo root; adjust as needed for your environment.

5. Tell me to restart my Codex session so the updated ~/.codex/config.toml
   is picked up.

Notes for you (the agent):
- The JSON payload shape is:
  {"detected": {...}, "planned": [...], "applied": {...}, "verify": {"status": "...", "detail": "...", "mcp_note": "..."}, "degraded": bool}
- Codex MCP config lives in ~/.codex/config.toml as [mcp_servers.memorymaster]
  TOML tables, not in ~/.claude.json. The installer writes a marker-bounded
  block so re-runs are idempotent and your existing config is preserved.
- The MCP server command registered is the same non-deprecated path:
  python -m memorymaster.surfaces.mcp_server
- If degraded is true (Docker absent / services unreachable) the install still
  succeeded (exit 0). SQLite-only mode is fully functional; Qdrant index
  maintenance and local LLM auto-ingest are off. Starting Qdrant later does not
  lift the R1.3 payload-retrieval quarantine.
- Claude Code hooks (UserPromptSubmit, Stop, SessionStart, PreCompact) are NOT
  registered for Codex — those are Claude Code-specific. The session-end script
  above is the Codex equivalent for distilled ingest.
```

---

## Flag reference (for advanced / manual use)

| Flag | Effect |
|---|---|
| `-y` / `--yes` | Non-interactive; accept all defaults, no prompts |
| `--db PATH` | Path to `memorymaster.db` (default: `<project-root>/memorymaster.db`) |
| `--provider {google,openai,anthropic,ollama}` | LLM provider for the auto-ingest Stop hook |
| `--api-key KEY` | API key for the chosen provider |
| `--model MODEL` | LLM model id |
| `--project-root PATH` | Directory where `memorymaster.db` lives |
| `--full-stack` | Bring up the Qdrant maintenance index + Ollama via Docker Compose (default when omitted) |
| `--no-full-stack` | Skip the Qdrant-index + local-LLM stack |
| `--no-cron` | Skip steward cron setup |
| `--no-obsidian-skills` | Skip Obsidian skills install |
| `--codex` | Force Codex MCP + instructions wiring (auto-detected otherwise) |
| `--no-codex` | Skip Codex wiring |
| `--force` | Overwrite existing MCP entries (default is brownfield-safe skip) |
| `--verify-only` | Run only the sentinel round-trip and exit |
| `--json` | Emit machine-readable JSON result to stdout; human output goes to stderr |

## Degraded mode

If Docker is absent and Qdrant/Ollama are not already reachable at
`QDRANT_URL`/`OLLAMA_URL`, the installer continues in SQLite-only mode:

> Running in SQLite-only mode. Qdrant index maintenance + local LLM auto-ingest are OFF.
> Retrieval remains available through authoritative SQLite ranking. To enable index
> maintenance or local LLMs, use `--full-stack` or QDRANT_URL / OLLAMA_URL.

The exit code is still 0. Core claim storage, recall hooks, and MCP tools
remain fully functional. Qdrant payload retrieval remains quarantined even when
the optional service is available.
