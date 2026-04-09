# Debate 002 Complete Synthesis: /project-setup v2

## 5-Round Results

| Round | Focus | Key Outcomes |
|-------|-------|-------------|
| R1 | Find problems | 16 unique issues across 3 advisors |
| R2 | Classify severity | Showstoppers vs operational flaws vs nice-to-haves |
| R3 | Challenge | Gemini called Claude's bash defense "delusional"; Claude proved npm test IS destructive; Codex called Gemini's Python abstraction "cargo cult"; Claude found race condition gap |
| R4 | Converge | All 3 agree on 3 mandatory changes + 6 should-fix items |
| R5 | Final verdict | Consensus at 90-98% confidence |

## UNANIMOUS (3/3) — MUST FIX before rollout

### 1. Ban destructive verification
- Delete all instructions to run test commands
- Replace with safe-probe-only allowlist: `--help`, `--version`, `--co`, `--no-run`, `--listTests`, file existence
- Explicitly forbid: `npm test`, `pytest` (without `--co`), `go test`, migrations, seeders, network calls
- **Evidence**: Claude confirmed real destructive hooks in whatsappbot-prod (DB seed) and pedrito (real API calls)

### 2. Idempotency markers
- Insert `<!-- project-setup:YYYY-MM-DD -->` in generated files
- On re-run: detect marker → UPDATE mode (refresh Commands, Testing, Verification only)
- Skip MemoryMaster query on re-run to prevent feedback loops (re-summarizing own output)
- `--force` overrides to full regeneration
- **Evidence**: Gemini identified "curation bloat" feedback loop; Codex called it "a ticking time bomb"

### 3. Lock scope on first run
- First run: derive from git remote URL, write to AGENTS.md as hardcoded string
- Re-run: `grep "Scope:" AGENTS.md` → use existing, never re-derive
- Prevents scope drift + prevents two unrelated repos collapsing to same slug
- **Evidence**: Codex R1 identified slug collision for generic names (api, web, worker)

## MAJORITY (2/3) — SHOULD FIX

### 4. --force needs managed/unmanaged section model (Codex, Claude)
- Don't replace whole file — separate managed sections (generated) from unmanaged sections (hand-edited)
- Use markers: `<!-- managed:start -->` / `<!-- managed:end -->`
- `--force` only rewrites managed sections, preserves unmanaged

### 5. --dry-run contract must be explicit (Codex)
- Define: explores codebase YES, queries MCP YES, writes files NO
- Shows diff of proposed changes vs existing files
- If exploration itself fails (EPERM, missing tools), report that in dry-run output

### 6. Rollback must also clean up new files (Codex)
- `git stash pop` restores old files but doesn't delete newly created GEMINI.md, .gemini/
- Add cleanup: `git clean -f GEMINI.md .gemini/settings.json .claude/rules/*.md` for files that didn't exist before

### 7. Exploration must hard-ignore problem dirs (Codex, Gemini)
- Add explicit exclusions: `node_modules`, `.pytest_cache`, `.next`, `target`, `vendor`, `.git`, `dist`, `build`, `__pycache__`
- Gemini R1 hit EPERM on `.pytest_cache` — this is a real failure in our own repo

### 8. Error handling between steps (Claude)
- If step 3 (explore) returns zero useful info, warn and ask user before generating
- If MCP query fails, note it and continue — don't silently generate without memory context

### 9. Race condition awareness (Claude R3)
- Add note: "Do not run /project-setup simultaneously from multiple agents"
- Not fixable in a markdown skill — would need file locking in a Python script

## DISPUTED (1/3) — REJECTED for now, reconsider for open-source

### 10. Bash-only portability (Gemini, 5 rounds)
Gemini insisted this is a showstopper. Claude and Codex argued it's irrelevant for our Win+GitBash environment. **Verdict**: Skip for internal rollout. Revisit if open-sourcing.

### 11. Glob-based discovery (Gemini)
Replace shell with agent-native glob. **Verdict**: Over-engineering for instructions. The bash commands are examples Claude follows, not a program to port.

### 12. Atomic writes (Codex)
**Verdict**: Single-user file writes don't need locking.

### 13-16. Minor items
- MemoryMaster coupling: handled by graceful degradation
- Context fragmentation: `.claude/rules/` is better than monolith (Claude proved this)
- Exclude MemoryMaster: add `--no-memorymaster` flag later if needed
- Line limit: make it a guideline (~150) not a hard cap

## Advisor Confidence

| Advisor | Start | End | Shift |
|---------|-------|-----|-------|
| Claude | 75% | 90% | +15% |
| Gemini | 85% | 98% | +13% |
| Codex | 75% | 97% | +22% |

## Implementation Checklist

- [ ] 1. Ban destructive verification (MUST)
- [ ] 2. Add idempotency markers (MUST)
- [ ] 3. Lock scope on first run (MUST)
- [ ] 4. Managed/unmanaged section markers (SHOULD)
- [ ] 5. Explicit --dry-run contract (SHOULD)
- [ ] 6. Rollback cleans new files (SHOULD)
- [ ] 7. Hard-ignore problem dirs in exploration (SHOULD)
- [ ] 8. Error handling between steps (SHOULD)
- [ ] 9. Race condition warning (SHOULD)
