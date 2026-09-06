<!-- doc-head: 4.8.8 useful-memory implementation and acceptance evidence -->
# Useful memory, reliable delivery
Covers: supported recall installation, real CI evaluation and source-level Dreaming sampling.
Key terms: Gemini-only, duplicate delivery, missed facts, label provenance, read-only.
Read when: accepting or deploying 4.8.8; ROADMAP.md remains the sole roadmap.
Status: merged and locally deployed 4.8.8; existing stdio clients still need reconnect.
<!-- /doc-head -->

## Product changes

1. Supported recall hooks skip only known machine-event prefixes; discussing a
   marker mid-sentence still recalls. Identical context is suppressed for at most
   five minutes, keyed by full session identity and project context. Changed
   context/new session delivers. SessionStart, including compaction/resume, resets.
2. Delivery state contains hashes/time only and is written after flushed output.
   Corrupt/unavailable state degrades to delivery. It is best-effort context
   optimization, not exactly-once concurrent transport or proof an agent read it.
3. Setup preserves unknown/custom recall hooks and emits a `.proposed` file.
   A full-body fingerprint recognizes untouched managed hooks; local edits are
   preserved too. Existing custom look-ahead functionality is not overwritten.
   Windows path substitution uses forward slashes, fixing generated Python
   escape failures witnessed by the installation round-trip.
4. CI's evaluation job uses the existing qrels and disposable public lifecycle;
   failures and missing JUnit artifacts fail the job. The old manual
   `eval_memorymaster.py` remains a legacy tool requiring explicit case files,
   not a working default CI/acceptance proof.
5. Dreaming's existing evaluator adds useful-selection precision/recall and
   missed/unwanted counts. Duplicate IDs invalidate the evaluation. Only explicit
   `label_origin: human` plus boolean `human_accept` counts for the existing
   human gate; AI/synthetic/unknown origins do not become human review.
   This is an offline label contract, not authentication of the label author.
6. The read-only sampler inventories all capture strata and chooses a bounded
   deterministic sample. It emits capture IDs, counts and fingerprints, never
   source/claim text. It creates no ledger and changes no historical claims.

## How to evaluate

Run `python scripts/sample_dreaming.py --help` for a timezone-aware inclusive/
exclusive source window and optional JSON artifact. Default five per stratum,
maximum one hundred; the entire population contributes to the fingerprint.
Run `python scripts/evaluate_dreaming.py labels.jsonl` for labeled decisions.

Each evaluation record must have a unique nonempty record_id, explicit booleans
should_emit/emitted/structured_valid; emitted records also require evidence_exact,
expected_scope/actual_scope and expected_action/actual_action. Human acceptance
needs label_origin=human and a boolean human_accept. Record one expected fact per
source-supported proposition, including useful facts that produced no candidate,
plus unwanted emissions and routine negative controls. “Should emit” must be
judged against the source, not inferred from the output.

The added thresholds are useful_precision >= 0.90 and useful_recall >= 0.85;
existing evidence/scope/action/structured/sample/human gates remain. These are
acceptance targets, not achieved production scores. Useful selection is measured
separately from citation entailment and factual correctness. The evaluator never
promotes a claim or activates a worker.

The earlier 88-action Gemini review is diagnostic AI evidence and cannot supply
human acceptance labels. No new Gemini call or source-text egress was needed here.

## Actual source population

Measured through SQLite mode=ro, query_only and a consistent read transaction,
for 2026-08-29T15:38:34.710902Z through 2026-09-05T15:38:34.710902Z:

- 188 captures: 118 zero-candidate, 10 with candidates/no recorded actions,
  60 with candidates/recorded actions; 15 selected source captures.
- Source-population fingerprint:
  `7961c8cb75207b990210368d830c77b71ad05b6b07d7eb37e1250e9f0ff15a88`.
- Source selection lives in ignored `artifacts/useful-memory/source-sample.json`.
  These are counts, not proof that the 118 captures contain missed useful facts.
  Precision and useful recall remain explicitly unknown until source labeling.
- Captures may be repeated windows from the same session; they are not 188
  independent human conversations. Action counts are not confirmed memories.

## Verification

- Acceptance checklist: [GATES.md](../GATES.md).
- Initial failing witnesses: missing delivery module; nine evaluator failures
  (missing omission metrics, AI labels counted as human, malformed labels);
  installation round-trip exposed Windows escape failure; editing a managed hook
  initially lost customization, then the digest-based recognition fixed it.
- Current focused tests: 32 passed; delivery/evaluation/sampling coverage 94%
  combined (individual modules 91%, 95%, 96%).
- Installation/source-steward/qrels/public-lifecycle integration: 83 passed.
- Ruff passed. Twelve release-truth tests and generated metadata verification passed.
- Wheel built and installed in a clean environment; isolated 4.8.8 imports and
  delivery/evaluation smoke passed without importing the source checkout.

## Scope, status and next decision

This milestone contains no database migration, backend replacement, ranking
change, extra steward agent, scheduler, provider switch, feature activation or
historical curation. Gemini remains the selected deployment provider. The
upstream ideas are patterns, not copied runtime code.

Next: label a bounded sample including zero-output sources, then keep one
retrieval or extraction-rescue experiment only if it improves matched source
outcomes. Persistent quota cooldown requires evidence of repeated cross-cycle
quota failures; more state is not added speculatively. No 24-hour wait is an
implementation prerequisite.

## Deployment evidence - 2026-09-06 UTC

- PR #252 passed all fifteen checks, including the six-platform/Python matrix,
  ML, performance, real evaluation artifacts and deployment smoke. Merged into
  main as `39bacf871b5fd75737597d23f988da4bd62af30b`.
- Built from merged main; wheel SHA-256:
  `F1465722B70BE8E9E36FD1C1BB044D6225A398C8C298A0BEFBF1CA982FC506E6`.
  Installed 4.8.8 with no dependency changes in the general and dedicated
  scheduled runtimes. Prior 4.8.7 wheel and pre-change custom hooks retained.
- Restarted only the managed HTTP MCP task. Fresh installed runtime reports
  health/readiness 200, unauthenticated rejection 401, 51 tools and successful
  authorized read-only recall. Startup was found in the configured log with
  zero error/traceback lines in the post-start tail.
- A separate fresh installed stdio process passed initialize/tool discovery
  with 51 tools using a disposable workspace; no authoritative-row test writes.
- Installed-package demo passed candidate promotion, cited opt-in observation
  recall, ordinary exclusion and automatic staleness after support retirement.
- Reconciled the existing custom recall hook with bounded combined-context
  delivery, preserving task look-ahead. Its duplicate-delivery witness failed
  before reconciliation and passed afterward, including real installed
  SessionStart reset against a missing disposable DB. Hooks apply next event.
- Nine older stdio MCP processes belonging to eight live agent processes were
  left intact. Reconnect MemoryMaster MCP or restart/resume those sessions,
  including this one; a package install cannot reload their imported modules.
  No workstation or terminal-host restart is required. HTTP is already restarted.
- Scripts, disposable test results and hook rollback copies are retained under
  ignored `artifacts/deploy-4.8.8/`. No GitHub release tag or PyPI publication,
  historical curation, provider change, new scheduler or database migration.
