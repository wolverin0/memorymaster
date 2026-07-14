# MemoryMaster — Agent Install Prompt

Copy the appropriate block below and paste it as a prompt to your coding agent.
The agent will run the installer, read the machine-readable result, and report
what was wired, what was reused (brownfield), and what degraded.

MemoryMaster's primary profile is personal/local: SQLite plus private stdio
MCP. Postgres/team is deferred. Qdrant is optional and governed; enabling it
requires an explicit semantic profile and does not make it authoritative.

---

## Claude Code variant

```
Install MemoryMaster in this environment. Follow these steps exactly.

1. Install the personal/local package extras:

   pip install "memorymaster[mcp,security]"

2. Run the detect-first, non-interactive installer and capture the JSON output:

   memorymaster-setup --yes --profile minimal --no-full-stack --json

   The installer prints human-readable progress to stderr and emits a single
   JSON document to stdout. Parse the JSON and report back to me:

   - detected: which clients and optional tools were found. Docker, Qdrant,
     Ollama, and Postgres may be reported but must not be started for minimal.
   - planned: the list of [will-do / skip-present / cant-missing] lines the
     installer printed — paste them verbatim so I can see the plan.
   - applied: what was actually wired (hooks, mcp_claude, cron, full_stack, etc.).
   - degraded: true/false. Missing optional Postgres, Qdrant, Ollama, or Docker
     must not make the minimal profile degraded.
   - verify.status: PASS, PARTIAL, or FAIL and the detail string.

3. Run the verify round-trip to confirm hooks and DB are functional:

   memorymaster-setup --verify-only

   Show me the output.

4. Tell me to restart my Claude Code session. MCP and hooks take effect only
   after a full session restart.

Notes for you (the agent):
- The JSON payload shape is:
  {"detected": {...}, "planned": [...], "applied": {...}, "verify": {"status": "...", "detail": "...", "mcp_note": "..."}, "degraded": bool}
- Do not add `--full-stack`, Postgres, Qdrant, or provider extras unless I ask
  for a different profile.
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

1. Install the personal/local package extras:

   pip install "memorymaster[mcp,security]"

2. Run the detect-first, non-interactive installer and capture the JSON output:

   memorymaster-setup --yes --profile minimal --no-full-stack --codex --json

   The --codex flag ensures ~/.codex/config.toml is updated with the MCP
   server entry (Codex reads MCP config from there, not from ~/.claude.json).
   Parse the JSON and report back to me:

   - detected: Codex presence and MCP registration state. Optional services
     may be reported but must not be started.
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
- The minimal SQLite profile is fully functional without Docker, Qdrant,
  Ollama, or Postgres. Their absence is not degraded local operation.
- Claude Code hooks (UserPromptSubmit, Stop, SessionStart, PreCompact) are NOT
  registered for Codex — those are Claude Code-specific. The session-end script
  above is the Codex equivalent for distilled ingest.
```

---

## Flag reference (for advanced / manual use)

| Flag | Effect |
|---|---|
| `-y` / `--yes` | Non-interactive; accept all defaults, no prompts |
| `--profile {minimal,semantic,team,full-lab}` | Select the profile to verify; defaults to `minimal` |
| `--db PATH` | Path to `memorymaster.db` (default: `<project-root>/memorymaster.db`) |
| `--provider {google,openai,anthropic,ollama}` | LLM provider for the auto-ingest Stop hook |
| `--api-key KEY` | API key for the chosen provider |
| `--model MODEL` | LLM model id |
| `--project-root PATH` | Directory where `memorymaster.db` lives |
| `--full-stack` | Bring up optional Qdrant + Ollama for `semantic`/`full-lab` |
| `--no-full-stack` | Skip the Qdrant-index + local-LLM stack |
| `--no-cron` | Skip steward cron setup |
| `--no-obsidian-skills` | Skip Obsidian skills install |
| `--codex` | Force Codex MCP + instructions wiring (auto-detected otherwise) |
| `--no-codex` | Skip Codex wiring |
| `--force` | Overwrite existing MCP entries (default is brownfield-safe skip) |
| `--verify-only` | Run only the sentinel round-trip and exit |
| `--json` | Emit machine-readable JSON result to stdout; human output goes to stderr |

## Optional semantic mode

The minimal profile neither requires nor starts Docker/Qdrant/Ollama. If an
explicit semantic/full-lab setup cannot reach those optional services, it
continues with authoritative SQLite retrieval and reports:

> Running in SQLite-only mode. Qdrant index maintenance + local LLM auto-ingest are OFF.
> Retrieval remains available through authoritative SQLite ranking. To enable index
> maintenance or local LLMs, use `--full-stack` or QDRANT_URL / OLLAMA_URL.

Core claim storage, recall hooks, and MCP tools remain fully functional. When
governed Qdrant reads are explicitly enabled, candidates are still rehydrated
and filtered through the authoritative store.
