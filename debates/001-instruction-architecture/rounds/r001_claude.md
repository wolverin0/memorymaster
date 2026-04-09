# Claude — Round 1

**Position:** Architecture is ready for rollout with 3 targeted fixes, not a redesign.

**Key Arguments:**
- The 4-layer model (global/project/path/ephemeral) is correct and aligns with every reference (Karpathy, Farza, GBrain, the intent doc). No one disputes this.
- AGENTS.md as canonical + CLAUDE.md as thin wrapper is the right pattern. Codex reads AGENTS.md natively, Claude reads CLAUDE.md which imports it.
- The project-setup skill produces verified, real-world output (tested on memorymaster: 80 lines, all commands work, all paths exist).
- MemoryMaster integration is the differentiator — no other system has automatic recall hook + stop hook + steward cron + wiki absorb. This is beyond what Karpathy described.

**But Gemini is right about 3 things:**
1. GitNexus bloat — the auto-generated section is 70 lines appended to AGENTS.md. Should move to `.claude/rules/gitnexus.md` or stay only in CLAUDE.md.
2. No GEMINI.md — Gemini CLI users get nothing. A 3-line wrapper or `.gemini/settings.json` with `context.fileName: ["AGENTS.md"]` fixes this.
3. The project-setup skill should add a "Verification" section to generated AGENTS.md — what commands to run to verify the project works.

**Assumptions:**
- `@AGENTS.md` import works in Claude Code (it does — tested in this session)
- Codex reads AGENTS.md from project root (confirmed by Codex docs)
- Most projects have < 200 lines of real instructions

**Risks / Failure Modes:**
- Instruction drift if project-setup isn't re-run after major architecture changes
- MemoryMaster section in global CLAUDE.md might be ignored because it's too long (30+ lines)
- Cross-project claims leaking into wrong scopes (the dream-seed bug we already fixed)

**What Would Change My Mind:**
- Evidence that the `@AGENTS.md` import doesn't work reliably in subagents or hooks
- A project that genuinely needs > 200 lines of instructions and can't use .claude/rules/

**Confidence: 82%**
