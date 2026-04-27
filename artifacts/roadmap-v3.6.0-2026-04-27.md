# Roadmap v3.6.0 — Cleaner entity graph + bigger eval

**Branch**: `omni/v3.6.0-l2-v3-and-n1000`
**Trigger**: B1 (v3.5.2) found GRAPH stream flat with N=100 prompts, +0.002 lift in noise. Two root causes hypothesized: (a) L2 entity backfill from 2026-04-25 over-extracted noise (some haiku batches returned 5-7×/claim), (b) N=100 eval set is too small to detect weight-tuning lifts.

## Goal

Resolve both hypotheses in one ship:
1. Re-extract entities with a tighter L2 prompt v3 → cleaner graph → GRAPH stream may light up.
2. Generate N=1000 labeled prompt set → noise floor drops → autoresearch can detect real lifts.

Then re-run B1 with both improvements. If GRAPH stream still flat with the bigger eval and cleaner graph, the next-lever is the `1/(1+hops)` formula itself, not the inputs.

## Plan (5 waves, parallel where possible)

### Wave 1 — Independent, parallel

| Task | What | Spawn |
|---|---|---|
| 1a | Design L2 prompt v3 (max 5 ents/claim, negative examples, bump LLM_PROMPT_VERSION) | foreground (single file edit + smoke test) |
| 2a | Generate N=1000 synthetic prompts via 5 parallel haiku subagents (200 each) | background subagents |

### Wave 2 — Depends on Wave 1

| Task | What | Spawn |
|---|---|---|
| 1b | Re-run L2 backfill with v3 prompt (60 batches × 200 claims via 3 parallel haiku) | background subagents |
| 2b | Label N=1000 prompts via LLM-judge against current recall top-50 | background subagents |

### Wave 3 — Depends on Wave 2

| Task | What |
|---|---|
| 1c | Re-run B1 grid post-v3 backfill (N=100 prompts, see if GRAPH lights up) |
| 2c | Re-run B1 grid with N=1000 prompts (with whichever entity graph survives) |

### Wave 4 — Optional based on results

| Task | What |
|---|---|
| 4 | Wiki re-absorb full + cleanup (uses cleaner v3 entity context) |
| 5 | Autoresearch B2 (graph hops formula sweep), B3 (prompt v3 quality measurement), B4 (wiki freshness signal) — three parallel worktree subagents |

### Wave 5 — Cross-project (independent of waves 2-4, but heavy)

| Task | What |
|---|---|
| 3 | Install memorymaster + L2 backfill on Tier A 8 projects (sequential, may require interactive `memorymaster-setup`) |

### Wave 6 — Ship

| Task | What |
|---|---|
| 6 | v3.6.0 release: CHANGELOG, branch + commit + ff-merge + push + tag + wheel + PyPI + GitHub Release |

## Acceptance criteria

- L2 v3 produces fewer entities per claim on the same input (target: 1.5-2.5/claim avg, vs 4-7 in worst v2 batches).
- N=1000 prompts generated with realistic distribution (mix of architecture, debugging, decision recall, project-specific lookups).
- B1 re-run produces a measurable result either way (positive lift, or definitive null with N=1000 reducing the noise floor below 0.001).
- v3.6.0 PyPI + GitHub Release Latest.

## Honest non-goals

- Cross-project install (#3) is gated on `memorymaster-setup` working non-interactively; if it requires user prompts I report and skip rather than asking the user.
- B5 (wiki freshness composite formula) requires hand-labeled "stale articles" set, not in scope tonight.
- No claim DB schema changes.

## Risks

- Synthetic prompts via LLM may overlap with the existing 100-prompt set (would inflate scores). Mitigation: dedup against `real-prompts-100.jsonl` SHA1 hashes.
- L2 v3 might extract FEWER entities AND NOT improve recall (means the v2 noise wasn't actually hurting). That's a valid null result.
- The Stop-hook auto-ingest may consume budget while subagents are running. Acceptable.

## Estimate

12-16h end-to-end if everything runs. Can ship at end of wave 3 (~6h) if cross-project (#3) and Wave 4 turn out to add no measurable value.
