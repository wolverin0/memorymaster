# Claude — R1: Problems Found

**Structural issues:**
- The skill is 150+ lines of markdown instructions. That's a LOT of context consumed just to run `/project-setup`. The skill itself violates the "concise is key" principle from skill-creator docs. Could be 60% shorter.
- Steps 0-10 are sequential but there's no error handling between steps. If step 3 (explore) fails, it still proceeds to step 6 (generate). Should have checkpoints.

**Scope derivation is fragile:**
- `git remote get-url origin` may return SSH URLs (`git@github.com:user/repo.git`) or HTTPS (`https://github.com/user/repo.git`). The sed patterns may not handle all formats.
- Some projects don't have a git remote (local-only projects). Fallback to basename works but the instruction is vague.

**The 150-line target is arbitrary:**
- Says "aim for ~150, hard cap at 250 for monorepos". But who enforces this? Claude will try but there's no automated check. And some projects genuinely need 300+ lines (Paperclip has 6 agents with governance rules that MUST be in the canonical file).

**Gemini support is unverified:**
- We create `.gemini/settings.json` with `context.fileName` but I've never tested if Gemini CLI actually reads this from the project root vs `~/.gemini/`. If it doesn't work, every project gets a useless file.

**The @AGENTS.md import:**
- Works in this session (verified). But what about in `.claude/rules/` files? Or in subagent prompts? The import is Claude Code specific — Codex and Gemini don't support it. GEMINI.md says `@AGENTS.md` but Gemini might not resolve that.

**Backup timestamp collision:**
- `$(date +%Y%m%d_%H%M%S)` — if you run it twice in the same second (unlikely but possible with scripts), the backup overwrites itself.

**No idempotency guarantee:**
- Running `/project-setup` twice should produce the same result. But since it queries MemoryMaster (which may have new claims), and reads existing files (which it just modified), the second run will produce different output. This is a feature or a bug depending on perspective.

**Missing: no way to exclude sections:**
- Some projects don't want the MemoryMaster section (external contributors). No way to skip it.
