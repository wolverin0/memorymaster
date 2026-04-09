# Codex Research Notes

## Position

The architecture direction is mostly correct, but the current implementation is not ready to roll out unchanged to 15+ projects. The core idea from `new 1.txt` is strong: one canonical shared project file, thin tool-specific wrappers, scoped rules, and ephemeral memory kept out of committed instructions. The drift happens in the generator and in the example output.

## 1. Four-layer memory governance

The 4-layer model is correct in principle:

- Global: personal workstyle and universal operating rules.
- Project: shared repo truth.
- Path-specific: scoped rules for folders/domains.
- Ephemeral: MemoryMaster claims, auto-memory, transient discoveries.

What is right:

- It cleanly separates stable policy from learned operational memory.
- It matches the original intent document almost exactly.
- It aligns with MemoryMaster’s own claim/wiki split: claims are write-time evidence, wiki is curated read-time synthesis.

What is wrong or incomplete:

- The project-setup skill does not actually generate the path-specific layer; it only mentions `.claude/rules/`.
- The example `AGENTS.md` still contains volatile facts that belong in ephemeral or generated docs, not canonical instructions: module counts, test counts, indexed symbol counts.
- The example leaks environment-specific detail (`192.168.100.186:6333`) even though the file says “never hardcode IPs.”

## 2. Does the generator produce what projects need?

Not yet.

- It only targets `CLAUDE.md + AGENTS.md`, while the original intent explicitly called for a cross-agent architecture including Gemini wrappers/settings.
- It says “no global rule duplication,” but the example project `AGENTS.md` still repeats large amounts of global policy from `C:/Users/pauol/.claude/CLAUDE.md`: execution style, verification, git discipline, memory usage.
- It says keep files under 200 lines combined, but the example `AGENTS.md` alone is already far beyond that once GitNexus guidance is included.
- It treats “project instructions” as a static template rather than a minimal instruction graph. For 15+ repos, this will drift fast.

The generator is good at repo facts (mission, stack, commands, boundaries). It is weak at instruction architecture.

## 3. MemoryMaster representation

Mostly strong, but overloaded.

- Strong: query-before-assuming, ingest non-obvious learnings, wiki as read layer, claims DB as write layer.
- Strong: the repo itself implements the Karpathy/Farza wiki pattern in `memorymaster/wiki_engine.py` with “compiled truth above the line, timeline below the line.”
- Weak: project instructions mix stable MemoryMaster policy with operational implementation detail (sync cadence, auto-dream behavior, exact tooling inventory). That belongs in docs or generated status, not in the canonical startup file.

## 4. Alignment with Karpathy / Farza / GBrain-style LLM wiki patterns

Alignment is good at the product level, but only partial at the instruction level.

- Good: thematic synthesis over chronology, append-only timeline, wikilinks, linter/cleanup/breakdown loop.
- Good: “wiki = curated read layer, claims = evidence write layer” is the right abstraction.
- Missing: instructions should explicitly treat canonical agent files the same way: stable synthesized truth only. Counts, temporary infra topology, and active tool inventory should not live there.

## 5. What should change before rollout

- Make `AGENTS.md` the only committed canonical project file.
- Generate thin `CLAUDE.md` and `GEMINI.md` wrappers only when required.
- Actually generate scoped rule files for path-specific guidance.
- Move volatile repo facts and tool inventories into `docs/agent-memory/` or generated status files.
- Remove environment-specific IPs/paths from committed project instructions.
- Enforce a stricter rule: project files contain durable constraints and commands only, not telemetry.

Bottom line: the model is correct; the current generator output is too bloated, too duplicated, and too environment-specific for safe multi-project rollout.
