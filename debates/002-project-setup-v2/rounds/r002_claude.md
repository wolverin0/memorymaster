# Claude — R2: Discussion

**Showstoppers (must fix before rollout):**

1. **Destructive verification** — Gemini is 100% right. Running `npm test` blindly in an unfamiliar project could trigger migrations, API calls, or delete data. Already fixed: changed to `--help`/`--co` variants. This is the most dangerous issue.

2. **Scope collisions** — Multiple repos named "api" all get `project:api`. Already fixed: derive from git remote URL first, fallback to clean basename. But this needs testing on real repos.

**Real but not showstoppers:**

3. **Bash-only commands** — Gemini flagged this but Claude Code runs in bash on all platforms (even Windows). The skill only runs inside Claude Code, not standalone. Not a real issue for our use case.

4. **maxdepth 2** — Already fixed to 3. Monorepos with 4+ levels deep are rare in our portfolio.

5. **Custom backup vs git stash** — Already fixed: using git stash. Gemini was right, this is simpler.

**Nice-to-haves (can do later):**

6. **Skill is too long** — I raised this but it's wrong. Skills only load when invoked, not at startup. The context cost is one-time per invocation.

7. **@AGENTS.md in Gemini** — Already fixed: GEMINI.md uses plaintext reference, not @import.

8. **No idempotency** — Running twice produces slightly different output because MemoryMaster has new claims. This is actually correct behavior — the instructions should evolve.

9. **No way to exclude sections** — Edge case for external contributors. Can add `--no-memorymaster` flag later.

**What I disagree with:**

- **Gemini's "MemoryMaster tight coupling"** concern is overblown. The MCP tool names haven't changed in months and the skill has "graceful MCP failure" handling. If MCP is down, it continues without it.

- **Gemini's "context fragmentation"** from .claude/rules/ — splitting 300 lines into 5 focused files is BETTER for Claude than one monolith, because Claude reads relevant rules per-path, not everything at startup.

**Priority order for fixes: 1 > 2 > 4 > 5 > 3 > rest**
