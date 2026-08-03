# MemoryMaster upstream hardening implementation — 2026-08-03
# Covers: execution checklist for queue completeness, honest extraction outcomes, capture health, and OAuth evaluation.
# Key terms: keyset enumeration, scope starvation, graph starvation, retryable output, coverage ledger, OpenCode OAuth.
# Read when: implementing or verifying the bounded reliability work selected from the 2026-08-03 upstream audit.
# Authority: implements the Now reliability and quality-gate commitments in `ROADMAP.md`; it is not a second roadmap.
# Boundaries: SQLite-first, no new runtime dependency, no live activation, no provider fallback, no public fifth verb.
# Status: IN PROGRESS; update every checkbox and evidence pointer in the same commit as the corresponding work.

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

- [ ] Add a content-free, scope-aware coverage report from existing tables.
- [ ] Count missing stages, expired leases, due retries, blocked codes, and
  completed jobs with partial diagnostics.
- [ ] Integrate the report into operations health, setup verification, and the
  Capture Inbox summary without adding a fifth public verb.
- [ ] Add invariant, scope-isolation, sensitivity, and dashboard tests.

### H4 — Evaluation-only OpenCode OAuth judge

- [ ] Add an opt-in OpenCode judge to evaluation scripts, defaulting to
  `openai/gpt-5.4-mini` with medium effort.
- [ ] Record provider, model, effort, OpenCode version, prompt hash, latency,
  and fixture identity.
- [ ] Keep the judge outside steward promotion and production extraction.
- [ ] Add hermetic command, malformed-output, timeout, and resume tests.

### H5 — Convergence and handoff

- [ ] Run focused tests red then green for each package.
- [ ] Run the complete non-ML suite, required retrieval/ML gates, Ruff,
  collection, `git diff --check`, release-truth, and package-content checks.
- [ ] Run disposable SQLite backup/restore and migration compatibility checks.
- [ ] Run GitNexus change detection before every commit and refresh the index
  after the final commit while preserving embeddings.
- [ ] Update this checklist, the upstream audit, and `DOCS-MAP.md` with final
  evidence; do not publish or activate live scheduling in this work package.

## Explicit deferrals

- Producer actor fields wait for a real Hermes or generic-producer caller;
  WhatsApp already persists sender identity.
- Streaming capture, compact MCP output, MCP-major compatibility, Qdrant work,
  daemons, hosted/team operation, and new third-party dependencies remain out
  of scope.
