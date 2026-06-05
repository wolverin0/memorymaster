---
name: mm-audit
description: Deep adversarial code audit via a dynamic workflow. Fan-out finders across correctness/concurrency/security/perf/contract dimensions × module groups, dedup, then 3-skeptic adversarial refute-verify, completeness critic, and a ranked HTML findings report. Use when the user asks for a "deep audit", "adversarial audit", "find all the bugs", "security+correctness sweep", or wants an exhaustive review that goes beyond a single-context pass. Read-only — produces a report, does not change code.
version: 1.0.0
---

# mm-audit — Deep Adversarial Audit (dynamic workflow embedded in a skill)

Runs a multi-agent **dynamic workflow** that audits a codebase exhaustively and
verifies its own findings adversarially, then renders a ranked HTML report. This
is the proven harness that surfaced 68 confirmed findings on MemoryMaster
(2026-06-01): finders fan out blind to each other, every finding is attacked by
3 independent skeptics, and a completeness critic looks for what was missed.

## When to use
- "deep audit", "adversarial audit", "is this ready to ship?", "find all the bugs"
- A security + correctness + concurrency + perf + contract sweep across many modules
- Any review where single-context **agentic laziness / self-preferential bias /
  goal drift** would undercut trust (the three failure modes dynamic workflows fix)

## How it works (phases)
1. **Sweep** — dimension-specialist finders (correctness, concurrency, security,
   perf, contract) × module groups, each blind to the others, schema-structured.
2. **Dedup** — merge near-duplicate findings into a unique set.
3. **Verify** — 3 independent skeptics per finding, prompted to REFUTE; a
   deterministic in-code tally keeps only findings a majority did not refute.
4. **Critic** — completeness pass: what modules/paths/risk-classes were missed.
5. **Synthesize** — executive rollup (top risks + recurring themes).

## Run it

1. **Scope the target.** Confirm the repo root and (optionally) which module
   groups matter. The embedded workflow's `GROUPS`/`DIMENSIONS`/`INVARIANTS`
   constants are MemoryMaster-tuned — for another project, edit the copy first
   (see "Adapting" below). Pace agents in small batches (the script already
   batches `SWEEP_BATCH=4` / `VERIFY_BATCH=6`) to avoid server-side rate limits.

2. **Launch the workflow** with the embedded script:
   ```
   Workflow({ scriptPath: ".claude/skills/mm-audit/deep-audit.workflow.js" })
   ```
   It runs in the background and returns a structured result:
   `{ stats, synthesis, completeness_gaps, confirmed[], dropped[] }`.

3. **Render the HTML report** from the confirmed findings (severity-ranked,
   filterable, each with file:line + why-it's-a-bug + trigger + suggested fix +
   skeptic vote tally). Generate it with a small Python script that reads the
   workflow's `.output` file and writes a self-contained HTML to
   `C:\Users\pauol\artifacts\YYYY-MM-DD-<slug>.html`, then open it. (Do NOT pull
   the full ~500KB result into context — let Python parse it.)

4. **Report** the headline stats + top "build-now" risks inline; offer a gated
   fix workflow (worktree-isolated, one finding per agent, re-verified + tested)
   as the follow-up — never auto-fix.

## Adapting to another repo
Copy `deep-audit.workflow.js`, edit three constants near the top:
- `REPO` — the target checkout path.
- `GROUPS` — `{name, dims, modules[]}` per module cluster.
- `INVARIANTS` — the project's must-hold rules finders check against.
Keep the phase structure and the 3-skeptic verify intact — that's what makes the
findings trustworthy.

## Guardrails
- **Read-only.** This audits and reports; it does not edit code.
- **Pace the fan-out.** ~14 concurrent Opus agents trips Anthropic server-side
  rate limiting ("temporarily limiting requests"); the script batches to ~4-6 in
  flight. Keep it that way.
- **Trust the tally, not the verdict prose.** The deterministic majority-refute
  tally in the script is the gate; per-finding verdict labels can skew pessimistic.
