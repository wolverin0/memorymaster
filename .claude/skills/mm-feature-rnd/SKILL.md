---
name: mm-feature-rnd
description: Feature R&D via a dynamic workflow. Mine a codebase's roadmap, scaffolded/gated features, ADRs, and novel ideas for net-new feature candidates; generate full design specs; score them with a judge panel on value/effort/risk/differentiation; synthesize a ranked HTML feature catalog. Use when the user asks "what should we build next", "feature ideas", "R&D", "find net-new features", or wants a decision-ready catalog of buildable features (not bug fixes). Read-only — produces designs + a catalog, writes no feature code.
version: 1.0.0
---

# mm-feature-rnd — Feature R&D Catalog (dynamic workflow embedded in a skill)

Runs a multi-agent **dynamic workflow** that discovers net-new feature
candidates from real project evidence, designs each one, scores them with a
judge panel, and renders a ranked HTML catalog you pick from. This is the harness
that produced a 17-feature scored catalog for MemoryMaster (2026-06-01).

## When to use
- "what should we build next", "feature ideas", "R&D session", "roadmap mining"
- You want grounded, buildable feature proposals (scored, with full specs) —
  NOT generic brainstorming, NOT bug fixes/refactors (those are out of scope).

## How it works (phases)
1. **Scout** — parallel agents mine: roadmap/backlog files, scaffolded/env-gated
   half-built features, ADRs/architecture for under-exploited primitives, and
   novel differentiators vs competitors. Curate to a deduped slate.
2. **Design** — one full design spec per candidate (problem, design on the real
   stack, surface, effort S/M/L/XL, risk, differentiation, test plan, deps).
3. **Judge** — 2 judges per design (product-value + build-pragmatics lenses)
   score value/effort/risk/differentiation; deterministic in-code tally.
4. **Synthesize** — executive "build-first" list + themes.

## Run it
1. **Confirm scope** — the repo + that this is features/improvements only (no
   bugs/cleanup). The embedded `SCOUT_SLICES`/`CONTEXT`/`GROUPS` are
   MemoryMaster-tuned; for another project edit the copy first.
2. **Launch:**
   ```
   Workflow({ scriptPath: ".claude/skills/mm-feature-rnd/feature-catalog.workflow.js" })
   ```
   Returns `{ stats, synthesis{executive_summary, build_first[], themes[]}, catalog[] }`.
3. **Render the HTML catalog** from the `.output` file via a small Python script
   (severity/score-ranked cards, filterable by verdict) to
   `C:\Users\pauol\artifacts\YYYY-MM-DD-<slug>.html`, then open it. Don't pull the
   full result into context.
4. **Present** the top "build-first" features inline; let the USER pick which to
   prototype. Prototyping is a SEPARATE gated step (worktree builds + tests) —
   never auto-build features.

## Adapting to another repo
Edit `REPO`, `CONTEXT` (project + competitors), and `SCOUT_SLICES` (which files
to mine) at the top of the workflow `.js`. Keep the judge-panel + tally intact.

## Guardrails
- **Read-only / design-only.** Produces specs + a catalog; writes no feature code.
- **Features only.** Drop anything that is a bug fix, test, refactor, or cleanup.
- **Trust scores, not verdict prose.** The 2-judge median can skew labels
  pessimistic; rank by the numeric score + the synthesis build-first list.
- **Verify "already exists" claims before building later.** Scout/design agents
  hallucinate both directions about what's already built — any follow-up build
  MUST grep/read the real code first (see ref mm-712f).
