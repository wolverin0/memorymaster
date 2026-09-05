<!-- doc-head: Source-aware Dreaming steward deployed as 4.8.7; fleet reconnection remains outstanding -->
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
- Early broad verification was incomplete and exposed old fixture assumptions.
  The historical-correction fixture now seeds an already-confirmed row, and
  the tiny CAS fixture includes source identity and citation lineage. Original
  correction and concurrency assertions remain unchanged. Final local Windows
  suite: 4,946 passed, 77 skipped, one expected failure, 90 ML items deselected.
- PR #251 CI passed all six platform/Python jobs, ML, performance and deployment
  smoke. CI ML: 89 passed, six skipped. The optional legacy eval job was nominally
  green but produced no artifact because three default benchmark files are
  missing. It is NOT quality evidence; no whole-system quality claim is made.
- GitNexus indexed this isolated worktree without embeddings (the primary
  index was untouched). Change detection reports medium risk at shared
  promotion boundaries. Exact Git diff is narrower than its name-based
  symbol report; provider, validator and transition callers are covered above.

## Limits and rollout

PR #251 merged as `4ffc3003aabbe466cb954bdbbd613d4eed19ab02` on September 5.
Version 4.8.7 is installed in both the general and dedicated scheduled runtimes.
The managed HTTP service was restarted; both fresh-process installations passed
the source-review promotion/replay smoke, and live HTTP health/readiness,
unauthenticated rejection and authenticated tool discovery pass. Existing peer
stdio MCP processes were not killed and still require reconnection.

No historical claims were rewritten. One obsolete blocked graph job was
cancelled with an audit event after exact support revalidation; the detailed
backup, installed-hook corrections and runtime evidence are recorded in
ENGINEERING-CONSOLIDATION-2026-09-05.md. This is local deployment, not publication
of a GitHub release or PyPI distribution. Review is point-in-time over the captured source snapshot;
future messages in other captures still require subsequent consolidation.
Filters minimize sensitive context but cannot prove arbitrary prose secret-free.
Do not claim production accuracy or fleet activation from these source tests.
Rollback is a source/package revert; additive review events may remain. Reverting
the guard also restores the old promotion weakness, so pausing Dreaming is safer
than deliberately bypassing review when troubleshooting.
