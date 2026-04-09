# Debate Synthesis: Unified Agent Instruction Architecture

## Advisor Summary

| Advisor | Position | Confidence | Key Concern |
|---------|----------|------------|-------------|
| Gemini | Ready for phased rollout with 3 patches | 85% | GitNexus bloat + cross-agent symlinks |
| Codex | Direction correct, generator not ready | ~75% | Example leaks IPs, repeats global rules, no Gemini support |
| Claude | Ready with 3 targeted fixes | 82% | GitNexus in wrong file, no GEMINI.md, verbose MemoryMaster section |

## Consensus (All 3 Agree)

1. **AGENTS.md as canonical shared workflow is the correct pattern.** No dispute from any advisor.
2. **4-layer memory governance (global/project/path/ephemeral) is correct.** All three validated this independently.
3. **MemoryMaster integration is the differentiator** — no other system has recall hooks + stop hooks + steward + wiki-absorb. This exceeds Karpathy/Farza's original proposal.
4. **GitNexus section should NOT be in AGENTS.md** — all three flagged this. It's 70+ lines that eat context. Move to `.claude/rules/gitnexus.md` or keep only in CLAUDE.md (where the hook auto-generates it).
5. **A GEMINI.md wrapper or .gemini/settings.json is needed** — Gemini CLI won't find AGENTS.md without explicit configuration.
6. **The `@AGENTS.md` import pattern is sound** but needs verification that it works in all Claude Code contexts (subagents, hooks, worktrees).

## Disputed Issues

| Issue | Claude | Gemini | Codex |
|-------|--------|--------|-------|
| Ready for rollout now? | Yes, with 3 fixes | Yes, phased | No, generator needs work |
| Example AGENTS.md quality | Good (verified) | Good but bloated | Leaks IPs, repeats globals |
| MemoryMaster section length | Too long (30+ lines) | Appropriate (passive utility) | Should be reference file |
| Path-specific rules | Mentioned but not generated | Should be optional | Generator should scaffold |

## Action Items Before Rollout

### Must Fix (all 3 agree)

1. **Move GitNexus out of AGENTS.md** — The auto-generated block goes in CLAUDE.md only (where the hook writes it). Remove from AGENTS.md template.

2. **Add Gemini support to /project-setup** — Generate either `.gemini/settings.json` with `context.fileName: ["AGENTS.md"]` or a 3-line `GEMINI.md` wrapper.

3. **Remove hardcoded IP from example** — The memorymaster AGENTS.md still has `192.168.100.186:6333`. Use env var reference instead.

### Should Fix (2/3 agree)

4. **Add "Verification" section to generated AGENTS.md** — Beyond just "Testing", include a verification checklist: what to run after any change to confirm the project works.

5. **Don't repeat global rules in project files** — The generator should explicitly exclude code quality, git workflow, verification habits (those live in global CLAUDE.md).

6. **Remove volatile numbers from AGENTS.md** — "54 modules", "974 tests", "3991 symbols" go stale. Use commands that compute these live instead.

### Nice to Have

7. **Scaffold `.claude/rules/` with empty templates** — The project-setup skill mentions path-specific rules but doesn't create them.

8. **Add last-steward-run timestamp** to wiki index so agents can gauge data freshness.

## Risks

| Risk | Raised by | Severity |
|------|-----------|----------|
| Instruction drift if project-setup not re-run | Gemini, Claude | Medium |
| `@AGENTS.md` import failure in subagents | Claude | Medium |
| Context bloat from GitNexus in 15 repos | All three | High |
| Codex sandbox errors during debate | Codex | Low (Windows-specific) |

## Recommendations

| Priority | Action | Source |
|----------|--------|--------|
| 1 | Move GitNexus to CLAUDE.md only, remove from AGENTS.md | All |
| 2 | Add Gemini support (.gemini/settings.json) | All |
| 3 | Remove hardcoded IPs from example AGENTS.md | Codex |
| 4 | Add Verification section to generated AGENTS.md | Gemini, Codex |
| 5 | Strip volatile counts from AGENTS.md template | Codex |
| 6 | Run /project-setup on 2 test projects before full rollout | Claude |

## Conclusion

The architecture is **correct and ready for rollout** after applying the 3 must-fix items (GitNexus relocation, Gemini support, IP cleanup). The 4-layer model, AGENTS.md as canonical, and MemoryMaster integration are all validated by 3 independent advisors. The main risk is instruction drift — mitigated by periodic `/project-setup` re-runs.

## Metadata
- Rounds completed: 1 (research + round 1; synthesis accelerated due to strong consensus)
- Early stop: yes (consensus on core issues after round 1)
- Models: Gemini (gemini-3-pro-preview), Codex (gpt-5.4), Claude (claude-opus-4-6)
