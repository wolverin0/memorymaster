# Claude Research Notes — Instruction Architecture

## What I found

### Global ~/.claude/CLAUDE.md (110 lines)
- STOP rules, Environment, MemoryMaster (expanded), MCP tools, Execution style, Code quality, Verification, Debugging, Git, AI Collaboration, Memory Governance (4 layers), Instruction Maintenance, When Stuck
- MemoryMaster section includes: query/ingest rules, wiki operations table, automatic flow description (recall hook, stop hook, steward cron, OpenClaw sync), MCP tool reference
- Memory Governance has the 4-layer model: Global → Project → Path → Ephemeral

### /project-setup skill
- 5-step process: explore codebase → query MemoryMaster → generate AGENTS.md → generate CLAUDE.md → verify
- AGENTS.md template: mission, stack, architecture, commands, boundaries, MemoryMaster scope, testing
- CLAUDE.md template: thin `@AGENTS.md` wrapper + Claude-specific notes
- Rules: under 200 lines, only real commands, no global duplication

### Example output (memorymaster/AGENTS.md — 80 lines)
- Has mission, stack, architecture table, commands table, boundaries, MemoryMaster scope, testing, key modules
- Clean, concise, all paths verified

### Original intent (new 1.txt)
- AGENTS.md as canonical shared workflow
- CLAUDE.md as thin wrapper importing AGENTS.md
- GEMINI.md optional wrapper
- 4-layer memory model (global/project/path/ephemeral)
- Anti-drift maintenance policy

## My position

**The architecture is 80% correct.** What's missing:

1. **No GEMINI.md wrapper** — The intent doc says Gemini can use AGENTS.md via settings.json `context.fileName`, but we haven't created any `.gemini/settings.json` for projects. Gemini sessions won't see AGENTS.md.

2. **No `@AGENTS.md` import actually works** — Claude Code's `@` import syntax loads referenced files, but I'm not sure it works across all contexts (subagents, hooks). Need to verify.

3. **The project-setup skill doesn't preserve GitNexus sections** — When regenerating CLAUDE.md, it might overwrite the auto-generated GitNexus section (between `gitnexus:start` and `gitnexus:end` markers).

4. **No Karpathy raw/ pipeline in the skill** — The project-setup skill doesn't create or configure the raw/ staging area for Obsidian Clipper ingestion per project.

5. **MemoryMaster in global CLAUDE.md is too long** — 30+ lines about wiki operations, automatic flows, tool reference. Most sessions won't need all that detail at startup. Could move to a reference file.

**Confidence: 75%** — Architecture is sound, but needs polishing before mass rollout.
