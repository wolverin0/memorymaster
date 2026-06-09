---
name: mm-mine-corrections
description: Mine your own correction history for recurring rules. A dynamic workflow harvests user-correction turns from the MemoryMaster verbatim corpus (857k+ rows), distills each to a prescriptive rule, clusters recurring themes, and proposes evidence-linked SKILLS / rule-claims (not CLAUDE.md bloat) — each backed by the source verbatim ids. Use when the user asks to "mine my corrections", "find the rules I keep repeating", "what do I keep telling Claude not to do", "turn my sessions into skills", or wants to harden behavior from real feedback. Read-only on the DB — proposes, does not auto-write rules.
version: 1.0.0
---

# mm-mine-corrections — Mine corrections → evidence-linked skills (workflow in a skill)

Artem Zhutov's "mine my last 50 sessions for the corrections I keep making"
pattern, pointed at **MemoryMaster's verbatim store** instead of raw logs — so
every proposed rule is **evidence-linked to the exact source verbatim rows**
(provenance a flat vault can't give you). This is the auto-extraction half of the
R1b backlog (rule-mining from corrections).

## When to use
- "mine my corrections", "the rules I keep repeating", "turn my sessions into skills"
- You want behavioral hardening grounded in your ACTUAL feedback, not guesses.

## How it works (phases)
1. **Harvest** — one agent runs READ-ONLY SQL over `verbatim_memories`, reusing
   `rule_miner._CORRECTION_KEYWORDS` / `_CORRECTION_FTS_MATCH` (production
   pre-filter) to pull up to N user-correction turns + their preceding assistant
   turn. Bounded — never loads 857k rows into context.
2. **Extract** — parallel Haiku agents (fast) distill each window to
   `{is_correction, trigger, instruction, rationale, theme}`, dropping non-corrections.
3. **Cluster** — one agent groups recurring corrections into themes (count>=2),
   each with a canonical rule + the member `source_verbatim_id`s as evidence.
4. **Propose** — per theme, decide the best home (**skill** > mm-rule-claim >
   claude-md-rule > drop) and write the actual prescriptive body.

## Run it
```
Workflow({ scriptPath: ".claude/skills/mm-mine-corrections/mine-corrections.workflow.js",
           args: { limit: 120, scope: null } })
```
- `args.limit` — how many correction windows to harvest (default 120; raise to mine deeper).
- `args.scope` — optional `project:<slug>` filter (default null = all scopes).

Returns `{ stats, proposals[] }`; each proposal = `{ theme, count, recommendation,
name, where, body, why_skill_not_claude_md, evidence_verbatim_ids }`.

After it returns:
1. **Render an HTML review** (recurring themes ranked by count, each with its
   proposed skill/rule body + the evidence verbatim ids) to
   `C:\Users\pauol\artifacts\YYYY-MM-DD-corrections.html`, then open it.
2. **Present the top recurring themes** inline. The USER decides which proposals
   to actually create — never auto-write skills/rules/CLAUDE.md from this.
3. For accepted ones: create the skill dir / `.claude/rules/*.md`, or
   `ingest_rule` for project-scoped rule-claims.

## Guardrails
- **Read-only on the DB.** Harvest uses `mode=ro`; nothing writes verbatim/claims.
- **Sensitivity:** the extract prompt forbids emitting secrets/paths; still, treat
  proposal bodies through the normal sensitivity lens before persisting.
- **Recurrence gate:** only themes with count>=2 surface; a single one-off "no"
  is noise, not a rule.
- **Prefer skills over CLAUDE.md** for behavioral corrections (the article's core
  lesson) — the Propose phase enforces this ranking.
- **Provenance is the differentiator:** keep `evidence_verbatim_ids` on every
  proposal so a human can trace each rule back to the real correction.
