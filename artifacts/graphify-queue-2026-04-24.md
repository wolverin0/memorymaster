# Graphify queue — legacy projects (2026-04-24)

**Roadmap item 9.1:** Run graphify on 15+ legacy projects.
**Status:** USER-INPUT — graphify is an interactive `/graphify` skill; there is no batch CLI mode (verified with `graphify --help` — only `query`, `save-result`, `benchmark`, `install`/`uninstall`, `hook install/status`).

## Scan result

Scanned `G:/_OneDrive/OneDrive/Desktop/Py Apps/` for git projects missing `graphify-out/`. **35 candidates** found (below).

## How to process this queue

For each project:

```bash
cd G:/_OneDrive/OneDrive/Desktop/Py Apps/<project>
claude   # open a Claude Code session
# Inside that session:
/graphify
```

One dedicated session per project. Graphify emits `graphify-out/GRAPH_REPORT.md` + `graphify-out/graph.json`. Processing time scales with source-code size — small tools (~5 min), large apps (~30–45 min).

## Candidate list (35 projects)

Suggested priority tiers — sort within each by what you use most:

### Tier A: High-value active codebases (graphify FIRST)
- `venezia/` — POS/backend-heavy, frequent edits
- `pedrito/` — Oracle + docker stack, active
- `impulsa/` — Supabase project, active
- `final-inpla/` — larger codebase per earlier memory
- `memoryking/` — relates to MemoryMaster architecture
- `openclaw2claude/` — sibling MCP server
- `wezbridge/` — pane automation, active
- `omniclaude/` (if git) — coordinator

### Tier B: Medium-value / periodic work
- `futura-command-center/`
- `argentina-sales-hub/`
- `clawtrol/`, `clawtrol-workspace/`
- `personaldashboard/`
- `app/`
- `nereidas/`
- `newspage/`
- `OCVSA/`
- `interonda/`
- `gimnasio/`

### Tier C: Small tools / experiments (low priority)
- `brandkit/`
- `cafebar/`, `caferesto/`, `elbraserito/` (similar restaurant tooling — graphify one, skip siblings)
- `companion/`
- `douglas-haig/`
- `fitflow-pro-connect2/`, `fitness-life-planner/` (similar — pick one)
- `goodmorning/`
- `ifbb-argentina/`
- `invoicescanner/`
- `lcdc/`
- `metasdk/`
- `montecino/`
- `mutual/`
- `Eye2byte/`
- `vibe-coders-os/`
- `wabwhite/`

### Skip (heavy external / non-code)
- `ComfyUI/` (third-party)
- `_archive/`, `_____testing/`, `backups/`, `VM_CLONES/`, `test-results/`

## Why autonomous execution wasn't feasible

- `/graphify` is a Claude Code SKILL, not a standalone CLI subcommand.
- It requires an interactive session context (the skill orchestrates multi-step work: discover sources, cluster, generate report, etc.).
- Spawning an agent-per-project is possible via `isolation=worktree`, but the graphify skill is global to the Claude Code install — it writes into the target project's `graphify-out/` from whatever session invokes it. Running 15+ in parallel would thrash disk + the shared skill state.

## Recommended batching

Tackle **Tier A (8 projects) over 1 focused day** (1 Claude session per project, ~30 min each = 4–5 hours). Tier B follows at 1–2 per day as needed. Tier C only if a specific question demands cross-project intelligence.

## Alternative: pick 15 yourself

If you have a shorter list in mind (e.g. "the 15 I actually touch weekly"), drop them here and I'll queue them first. My tiers above are guesses based on folder names + prior memory — you know which ones matter.
