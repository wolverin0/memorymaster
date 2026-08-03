# MemoryMaster upstream hardening implementation — 2026-08-03
# Covers: execution checklist for queue completeness, honest extraction outcomes, capture health, and OAuth evaluation.
# Key terms: keyset enumeration, scope starvation, graph starvation, retryable output, coverage ledger, OpenCode OAuth.
# Read when: implementing or verifying the bounded reliability work selected from the 2026-08-03 upstream audit.
# Authority: implements the Now reliability and quality-gate commitments in `ROADMAP.md`; it is not a second roadmap.
# Boundaries: SQLite-first, no new runtime dependency, no live activation, no provider fallback, no public fifth verb.
# Status: COMPLETE locally; the ancestor observation passed, but PR creation is withheld because the only configured remote is public.

## Outcome

Make governed capture complete and observable: every eligible item can be
enumerated, provider failures cannot look successful, and operators can verify
coverage without exposing captured content. Add optional OpenCode OAuth judging
only to evaluation surfaces after runtime correctness is green.

## User journeys

- As a local user, repeated `improve` calls eventually queue every eligible
  evidence item and confirmed claim without duplicate jobs.
- As an operator, I can distinguish valid empty extraction from partial,
  retryable, blocked, and structurally missing work.
- As an evaluator without API keys, I can run a bounded headless OpenCode judge
  and retain exact model, effort, prompt, and tool-version provenance.

## Execution checklist

### H1 — Complete queue enumeration

- [x] Add failing regression: requested-scope evidence survives more than 800
  earlier rows from other scopes.
- [x] Add failing regression: confirmed claim 201 is queued after the first 200.
- [x] Implement stable keyset enumeration without changing the schema.
- [x] Centralize graph-job identity and preserve replay-safe uniqueness.
- [x] Verify `max_items` caps newly queued work and repeated calls converge.

Evidence: `tests/test_public_v1.py` failed twice before the fix, then passed
11/11; the related capture/storage/graph set passed 26/26; focused Ruff and
`git diff --check` passed.

### H2 — Honest extraction outcomes

- [x] Add failing claim fixtures for all-invalid and partially-invalid JSON.
- [x] Add failing graph fixtures for timeout, empty output, malformed JSON,
  unknown ontology values, and valid empty output.
- [x] Add stable retryable error codes without silent provider fallback.
- [x] Preserve valid partial claims and record a completed-job diagnostic.
- [x] Verify five failed attempts become blocked and evidence is preserved.

Evidence: new tests failed at collection before the typed retry errors existed;
after implementation, the Atlas, capture-worker, entity-graph, ontology,
governed-storage, and Dreaming-surface set passed 85/85; focused Ruff passed.

### H3 — Capture coverage read model

- [x] Add a content-free, scope-aware coverage report from existing tables.
- [x] Count missing stages, expired leases, due retries, blocked codes, and
  completed jobs with partial diagnostics.
- [x] Integrate the report into operations health, setup verification, and the
  Capture Inbox summary without adding a fifth public verb.
- [x] Add invariant, scope-isolation, sensitivity, and dashboard tests.

Evidence: the focused coverage/operator/dashboard set passed 14/14; the wider
public capture, worker, setup-hooks, and operations set passed 69/69. The
serialized report test proves captured text is absent; focused Ruff and
`git diff --check` passed.

### H4 — Evaluation-only OpenCode OAuth judge

- [x] Add an opt-in OpenCode judge to evaluation scripts, defaulting to
  `openai/gpt-5.4-mini` with medium effort.
- [x] Record provider, model, effort, OpenCode version, prompt hash, latency,
  and fixture identity.
- [x] Keep the judge outside steward promotion and production extraction.
- [x] Add hermetic command, malformed-output, timeout, and resume tests.

Evidence: 12/12 focused evaluator tests pass. A real OAuth-only smoke call with
OpenCode 1.18.9 returned the exact `[7]` fixture through
`openai/gpt-5.4-mini` at medium effort; the result recorded model, effort,
version, prompt hash, latency, and token counts without exposing credentials.

### H5 — Convergence and handoff

- [x] Run focused tests red then green for each package.
- [x] Run the complete non-ML suite, required retrieval/ML gates, Ruff,
  collection, `git diff --check`, release-truth, and package-content checks.
- [x] Run disposable SQLite backup/restore and migration compatibility checks.
- [x] Run GitNexus change detection before every commit and refresh the index
  after the final commit while preserving embeddings.
- [x] Update this checklist, the upstream audit, and `DOCS-MAP.md` with final
  evidence; do not publish or activate live scheduling in this work package.

Evidence: the full suite passed 4,335 with 71 skipped and one expected failure;
4,407 tests collected. Focused retrieval/embedding tests passed 57/57 and
release-truth/supply-chain/e2e tests passed 85/85. The 500-question
LongMemEval-S rerun held R@5 at 0.9660 and R@10 at 0.9840 while MRR improved
from 0.9020579 to 0.9033913. Ruff and `git diff --check` passed.

The disposable SQLite drill applied migrations idempotently twice, preserved
one fixture claim, retained WAL, produced an encrypted authenticated backup,
and restored with `integrity_check=ok`, zero foreign-key violations, and RTO
met in 0.031 seconds. Clean default and `capture` wheel installs passed; the
922,291-byte wheel was bound to a validated CycloneDX 1.6 SBOM. Reviewed
full-history Gitleaks and strict OSV project/release-extra audits passed; the
capture environment also passed after updating only its disposable bootstrap
pip. No PostgreSQL, live database, scheduler, image, push, or publication was
touched. Final GitNexus change detection and reindex complete the local handoff.
The authoritative vNext audit records its replacement 24-hour observation as
passed. GitHub reports the sole configured `origin` as public, so no push or PR
was created under the operator's explicit no-publication boundary. A private
review remote, or later public-release authorization, is the only PR input left.

## Explicit deferrals

- Producer actor fields wait for a real Hermes or generic-producer caller;
  WhatsApp already persists sender identity.
- Streaming capture, compact MCP output, MCP-major compatibility, Qdrant work,
  daemons, hosted/team operation, and new third-party dependencies remain out
  of scope.
