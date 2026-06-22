# Spec — Install / Onboarding Rework

**Status:** DRAFT → building.
**Why:** install/onboarding is the #1 adoption blocker (claim `mm-300f`). 3 overlapping docs, no from-zero happy path, interactive-only setup that isn't detect-first, no verify, no full-stack orchestration.
**Locked decisions:** (1) BOTH — agent install prompt drives a detect-first idempotent setup, then verifies; (2) FULL-STACK default, friction absorbed by the agent, with a graceful no-Docker fallback; (3) Claude Code + Codex from v1; brownfield-reuse; consolidate docs.
**Constraint (STOP rule):** ENHANCE `setup_hooks.py` additively — do NOT rewrite from scratch. Preserve every existing wiring function and its idempotent remove-then-add behavior.

---

## 1. New module — `memorymaster/surfaces/setup_detect.py`

Pure, side-effect-free environment probing. Subprocess calls are read-only, `shell=False`, timeout-bounded (≤5s each), and NEVER raise (degrade to "unknown/absent").

```python
@dataclass(frozen=True)
class Detected:
    python_version: str
    pip_ok: bool
    os: str                      # "Windows" | "Linux" | "Darwin"
    docker: bool                 # `docker --version` ok
    docker_compose: bool         # `docker compose version` ok
    ollama: bool                 # http GET OLLAMA_URL/api/tags ok OR `ollama --version`
    ollama_models: tuple[str, ...]
    qdrant: bool                 # http GET QDRANT_URL/healthz ok
    obsidian_vault: str | None   # path if an obsidian-vault/ found under cwd
    gitnexus: bool               # `.gitnexus/` present OR `npx gitnexus` resolvable
    claude_code: bool            # ~/.claude/ exists
    codex: bool                  # ~/.codex/ exists
    mm_installed: bool           # `import memorymaster` works
    mm_mcp_registered: bool      # memorymaster already in ~/.claude.json mcpServers
    existing_hooks: tuple[str, ...]  # memorymaster hooks already in settings.json

def detect_environment(*, cwd: Path | None = None) -> Detected: ...
def format_plan(d: Detected, *, want_full_stack: bool) -> list[str]:
    """Human-readable 'will do / will skip (already present) / can't (missing dep)' lines."""
```

Brownfield = the report drives skip-if-present everywhere.

---

## 2. `setup_hooks.py` — additive enhancements

Keep all existing functions (`install_hooks`, `install_mcp`, `append_instructions`, `setup_steward_cron`, `install_obsidian_skills`). Add:

1. **argparse, non-interactive mode.** `main()` parses:
   `--yes/-y` (no prompts, accept defaults), `--db PATH`, `--provider {google,openai,anthropic,ollama}`, `--api-key`, `--model`, `--project-root PATH`, `--full-stack/--no-full-stack`, `--no-cron`, `--no-obsidian-skills`, `--codex/--no-codex`, `--verify-only`, `--json` (machine-readable result for the agent prompt). When a flag is given, never prompt for that value. `ask`/`ask_yn` must honor a global non-interactive flag (return the default/flag value instead of calling `input()`).
2. **Detect-first.** Call `detect_environment()` early; print `format_plan(...)`; in interactive mode confirm, in `--yes` proceed.
3. **Idempotent + brownfield.** Skip MCP/hook/cron/skill steps whose target is already present (use the Detected report). Re-running must be a no-op-ish (already idempotent for hooks — extend to MCP: don't clobber unless `--force`).
4. **Fix the MCP-path bug.** Register the server via the entry point: `command="memorymaster-mcp"` (preferred) or `[PYTHON_EXE, "-m", "memorymaster.surfaces.mcp_server"]` — NOT the deprecated `memorymaster.mcp_server`. Also register for Codex when `codex` detected (its MCP config location).
5. **Full-stack orchestration (the locked default).** New `setup_full_stack(detected, *, interactive, yes)`:
   - If `docker_compose` present: offer/run `docker compose up -d qdrant ollama` (reuse repo `docker-compose.yml`); wait for health; `ollama pull` the configured model. Reuse if `qdrant`/`ollama` already healthy (brownfield).
   - **No-Docker fallback:** if Docker absent AND Qdrant/Ollama not already running → print a clear, non-fatal message: "Running in SQLite-only mode. Vector recall + local LLM auto-ingest are OFF. To enable them: install Docker and re-run with `--full-stack`, or point QDRANT_URL/OLLAMA_URL at existing services." Setup continues and succeeds in degraded mode.
   - Never block the core install on stack failures.
6. **Verify step.** New `verify_install(db_path)`: init a throwaway check — ingest a sentinel claim then `query_memory` it back via `service`, confirm round-trip; if MCP registered, note "restart session to load MCP". Print PASS/PARTIAL with exactly what works. Reachable via `--verify-only`.
7. **`--json` output** for the agent prompt: emit `{detected, planned, applied, verify, degraded}` so the agent can parse and report.

---

## 3. Agent install prompt — `docs/AGENT-INSTALL.md`

A copy-paste block the user gives Claude Code **or** Codex. It instructs the agent to:
1. `pip install "memorymaster[mcp,security,qdrant,embeddings]"` (full-stack extras).
2. Run `memorymaster-setup --yes --full-stack --json` and read the JSON.
3. Report the plan (what was wired, what was reused/brownfield, what degraded).
4. Run `memorymaster-setup --verify-only` and show the round-trip result.
5. Tell the user to restart the session to load hooks + MCP.
Include a Claude-Code variant and a Codex variant (Codex MCP config path differs; Codex has no Stop hook → point at the session-end script, mirroring current `append_instructions`).

---

## 4. Docs consolidation

- **README.md:** replace the install section with a **30-second quickstart** — the single happy path: `pip install` → paste the agent prompt (link `docs/AGENT-INSTALL.md`) → restart → verified. One short "manual / advanced" pointer to `INSTALLATION.md`.
- **INSTALLATION.md:** stays as the reference matrix (extras, Docker, Helm, env vars, troubleshooting). Add the new flags + `--verify-only`.
- **docs/INTEGRATING.md:** remove install overlap; keep only the 3-beat agent contract / integration semantics. Cross-link, don't duplicate.

---

## 5. Tests (MANDATORY, hermetic)

All tests MUST patch `HOME`/`CLAUDE_DIR`/`CODEX_DIR` to `tmp_path` and mock `subprocess.run`/HTTP — **never touch the real `~/.claude`**. Cover:
- `setup_detect`: each probe parses canned output; all degrade to absent on timeout/error; no exceptions escape.
- `setup_hooks` non-interactive: `--yes --provider ... --db ...` wires hooks/MCP into a tmp HOME; re-run is idempotent (no duplicate hook entries; settings.json stays valid JSON).
- MCP registration uses the correct (non-deprecated) command.
- No-Docker fallback: Docker absent → setup still succeeds, degraded message emitted, exit 0.
- `verify_install`: round-trip ingest+query on a tmp DB returns PASS.
- `--json` emits valid parseable JSON.

---

## 6. Safety constraints
- Never corrupt existing `settings.json` / `.claude.json` — read-merge-write, preserve unknown keys, keep valid JSON on every path.
- All external-tool probes timeout-bounded, `shell=False`, never fatal.
- The build/test run must NOT execute the real installer against the developer machine.

## 7. Acceptance criteria (verifiable)
- [ ] `memorymaster-setup --yes --db <tmp> --provider ollama --no-cron --no-obsidian-skills` runs non-interactively to completion in a patched HOME, exit 0.
- [ ] Re-running it produces no duplicate hooks and identical settings.json (idempotent).
- [ ] MCP entry uses `memorymaster-mcp` / `surfaces.mcp_server`, not the deprecated path.
- [ ] Docker-absent path prints the degraded-mode message and still exits 0.
- [ ] `--verify-only` round-trips a sentinel claim (PASS) on a tmp DB.
- [ ] `--json` output parses.
- [ ] New + existing tests green; ruff clean; full suite collects clean.
- [ ] README quickstart ≤ ~30 lines to first verified memory; `docs/AGENT-INSTALL.md` present with Claude + Codex variants.
