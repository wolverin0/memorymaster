<!-- doc-head: Source-aware Dreaming steward, implemented in an isolated branch; not deployed -->
# Dreaming source steward: bounded implementation
Covers: original evidence review, promotion prerequisites, replay and verification.
Key terms: Gemini, chronology, modality, exact citations, audit receipt, candidates.
Read when: reviewing or deploying the September 5 source-aware steward change.
Authority: implementation ledger only; ROADMAP.md remains the sole roadmap.
<!-- /doc-head -->

## Change

- Reuse the existing Gemini consolidation call; no additional per-batch call,
  schema migration, service, pane, scheduler, or dependency.
- Supply sanitized capture messages, roles, timestamps and original scope.
  Include later messages in that capture so corrections are visible. Bound
  source context to 24,000 characters per candidate; oversized captures are
  explicitly incomplete and cannot receive an accepted review.
- Require six explicit checks: evidence, chronology, modality, scope,
  specificity and privacy. A question is not a preference; a plan is not a
  completed action; an uncertain statement is not an established fact.
- Persist accepted/rejected/needs-evidence reviews in existing audit events,
  with source and persisted claim/citation fingerprints, not raw messages.
  The normal steward remains responsible for promotion. Missing or changed
  reviews block confirmation; the text-only LLM steward cannot bypass this.
- Keep old unreviewed candidates pending. Do not rewrite already-confirmed
  historical rows. PostgreSQL Dreaming promotion remains explicitly deferred.
- Preserve cached decisions during crash replay instead of overwriting them
  with an empty list. Claims and review receipts replay without duplication.

## Evidence

- New regression suite initially failed because the review contract did not
  exist. Source-review suite now passes all 26 cases.
- Final focused Dreaming, Gemini-only providers, both stewards, lifecycle,
  atomic supersession and historical-correction suite: 136 tests passed;
  source-review module coverage 92% (99 statements, 8 uncovered).
- One real Gemini batch, model gemini-3.7-flash-low: 5/5 synthetic cases matched
  expectations. Questions, outdated facts, proposals and uncertainty were
  rejected; an explicit backup-retirement constraint was accepted. One call,
  21,130 input / 971 output tokens. Currency cost not measured.
- The real-provider smoke used synthetic text only and changed no memory rows.
  Five cases are a contract smoke, not a statistical precision benchmark.
- Ruff passes. Collection: 5,024 non-ML items, 90 ML items deselected.
- Broad non-ML run deliberately stopped after roughly ten minutes; INCOMPLETE,
  not a full-suite PASS. Its first failure was an old correction
  fixture promoting an unreviewed Dreaming claim. The fixture now explicitly
  seeds a historical confirmed row; the correction assertion is unchanged.
- GitNexus indexed this isolated worktree without embeddings (the primary
  index was untouched). Change detection reports medium risk at shared
  promotion boundaries. Exact Git diff is narrower than its name-based
  symbol report; provider, validator and transition callers are covered above.

## Limits and rollout

This branch is not installed or live. Existing task flags and historical data
are unchanged. Review is point-in-time over the captured source snapshot;
future messages in other captures still require subsequent consolidation.
Filters minimize sensitive context but cannot prove arbitrary prose secret-free.
Do not claim production accuracy or fleet activation from these source tests.
Rollback is a source/package revert; additive review events may remain. Reverting
the guard also restores the old promotion weakness, so pausing Dreaming is safer
than deliberately bypassing review when troubleshooting.
