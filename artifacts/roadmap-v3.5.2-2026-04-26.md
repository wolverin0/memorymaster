# Roadmap v3.5.2 — Audit + Autoresearch ship

**Branch**: `omni/audit-autoresearch-v3.5.2`
**Trigger**: post v3.5.0 + v3.5.1 release with new `claude_cli` provider, large L2 entity backfill (+8,229 entities), and hook env wiring fix. None of these were measured against recall.

## Goal

Close the audit loop on the v3.5.0/3.5.1 ship and use one autoresearch experiment to monetize the L2 backfill (turn raw entity count into measured precision@5 lift).

## Plan

### A — Mini-audit (defensive)

| Task | What | Why |
|---|---|---|
| A1 | Tests for `_call_claude_cli` provider | ~30 LOC of new code shipped to PyPI without tests. Cover: missing binary, non-zero exit, timeout, UTF-8 prompts with emojis, `MEMORYMASTER_CLAUDE_CLI_BIN` override, `MEMORYMASTER_CLAUDE_CLI_TIMEOUT` override. Mock `subprocess.run`. |
| A2 | Verify v3.5.1 wheel contains the new provider | Quick sanity that the published wheel actually has `_call_claude_cli` and `claude_cli` registered in `_PROVIDERS`. |
| A3 | Measure recall precision@5 with current entity registry | Snapshot the post-L2 baseline. Anchor for B1. |
| A4 | Regression test for hook env-assignment fix | Catches the 50× HTTP 404 bug if anyone ever reverts `os.environ[KEY]=...` back to `setdefault`. |

### B — Autoresearch experiment

| Task | What | Why |
|---|---|---|
| B1 | Grid search recall weights `W_LEXICAL × W_FRESHNESS × W_GRAPH × W_VECTOR` against precision@5 | Current weights are hand-set (`W_LEXICAL=0.3` was bumped manually 2 days ago). Picking a tuned set is cheap, ROI bumps recall directly. Goal: +5-15% precision@5 over A3 baseline. |

### C — Ship

| Task | What | Why |
|---|---|---|
| C | v3.5.2 release: roadmap doc + bump + branch + commit + ff-merge + push + tag + wheel + PyPI + GitHub Release | Close the loop, ship the audit + tuned weights together. |

## Acceptance criteria

- All 4 audit tasks pass (tests green, wheel verified, baseline captured, regression test added).
- B1 produces a measurable delta (positive or null) and the result is committed regardless — null is acceptable as long as we have the data.
- v3.5.2 on PyPI + GitHub Release marked Latest.
- CHANGELOG and `docs/handbook.md` updated where relevant.

## Honest non-goals

- No new features.
- No prompt v3 for entity extraction (deferred — backfill already applied).
- No wiki freshness experiment (blocked: no labeled stale-articles set).
- No graph hops formula tuning (deferred — coupled to B1, do after B1 ships).

## Risks

- B1 grid search may overfit on the existing labeled set (small N). Mitigation: report on held-out subset if available, otherwise call out as "indicative, not statistically significant" in the changelog.
- Subprocess mocking in A1 must match the real `subprocess.run` signature exactly or false-pass.

## Estimate

3h end-to-end. Audit ~1h, autoresearch ~1h, ship ~1h.
