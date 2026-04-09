# Claude — R5: Final Verdict

**Consensus reached on 3 mandatory changes:**

1. **Safe verification only.** Replace "run commands" with an allowlist: `--help`, `--version`, `--co`, `--no-run`, `--listTests`, file existence checks. NEVER run test suites, builds, migrations, or seeders. Already partially done — needs to be elevated to a WARNING section.

2. **Idempotency markers.** First run: insert `<!-- project-setup:YYYY-MM-DD:SCOPE -->` in generated files. Re-run: detect marker → only refresh Commands and Testing sections. Skip MemoryMaster query to prevent feedback loops. `--force` overrides to full regeneration.

3. **Scope lock.** First run: derive from git remote, write to AGENTS.md. Re-run: read existing scope from file, never re-derive. Prevents drift across runs.

**Rejected proposals:**
- Glob-based discovery (Gemini) — adds complexity, bash works for our environment
- Atomic writes with locks (Codex) — over-engineered for single-user file writes
- Python abstraction (Gemini) — the skill is instructions, not a program

**Confidence: 90%.** The 3 changes above address all showstoppers. Ready for rollout after applying them.
