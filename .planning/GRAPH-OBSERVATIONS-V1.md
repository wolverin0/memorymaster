<!-- doc-head: active PPR-7 graph-observations implementation ledger -->
# Graph Observations V1
# Covers: deterministic discovery, governed synthesis, lifecycle, opt-in recall, and rollout evidence.
# Key terms: exact signatures, union-find, support fingerprint, candidate observation, steward gate.
# Read when: implementing, reviewing, testing, or rolling back PPR-7.
# Status: implemented and locally verified; separate PR and CI evidence in progress.
<!-- /doc-head -->

## Fixed boundary

The feature derives optional observations only from current, confirmed,
non-sensitive claims with active evidence and graph-edge supports in one exact
tenant and scope. Components are deterministic; an LLM may summarize an
eligible component but cannot choose membership, cite outside it, promote its
own output, or feed observations back into extraction.

## Work ledger

| Area | Required outcome | Status |
|---|---|---|
| Migration 0020 | Add SQLite observation, support, and leased-job tables; PostgreSQL fails closed. | Implemented; migration tests pass |
| Discovery | Canonical signatures, hub suppression, union-find, bounds, deterministic fingerprints. | Implemented; 48-case evaluator is 100%/100% |
| Synthesis | Strict structured output, three-call cycle cap, provider/global budget, replay-safe `no_signal`. | Implemented; focused failure/cap tests pass |
| Lifecycle | Candidate-only creation, deterministic steward gate, immediate archive/stale on support change. | Implemented; promotion/retirement tests pass |
| Recall | Default byte-equivalent output; separately packed opt-in observations bounded to five. | Implemented; equivalence and opt-in tests pass |
| Surfaces | Python, CLI, MCP, dashboard, and disposable demo parity. | Implemented; focused surface tests pass |
| Evaluation | Versioned 40+ case corpus plus structural, citation, leakage, replay, and retrieval gates. | Local gates pass; PR CI pending |

## Acceptance evidence

- Structural component precision and recall are each at least 95% on the
  versioned corpus; observation precision is at least 90% and root-cause
  precision at least 85%.
- Citation/support correctness is 100%, with zero cross-tenant, cross-scope,
  sensitive, retired, stale, candidate, or observation-generated support.
- Ordinary recall is byte-equivalent when observations are disabled; graph
  top-five improves without more than 0.01 overall R@5/MRR regression.
- `improve()` only queues work and remains below 500 ms p95; an hourly scope
  cycle performs at most three observation synthesis calls.
- Focused tests, the full non-ML suite, retrieval gates, Ruff, collection,
  migration/restore, clean-wheel, supply-chain, and GitNexus checks pass before
a PPR-7 pull request is proposed.

## Local verification evidence

- Offline corpus: 48 cases, structural precision 1.00 and recall 1.00.
- Full non-ML suite: 4,463 passed, 72 skipped, 97 deselected, one expected
  failure; Ruff and 4,633-test collection pass.
- Focused restore/retrieval gates: 17 passed; release/supply-chain contract
  tests: 86 passed; regenerated release truth verifies.
- Clean wheel builds, passes Twine, installs into a fresh venv, initializes
  migration 0020, and completes the observation promotion/recall/staleness demo.
- Feature generation remains disabled by default; the configured Dreaming
  extraction/consolidation pair remains Gemini plus GLM.

## Rollout and rollback

Roll out through disposable SQLite and fake/local providers, feature-off wheel
installation, offline shadow evaluation, verified authoritative backup/restore,
then opt-in candidate generation. The 24-hour observation is post-implementation
operational evidence, not a coding prerequisite. Rollback disables generation
and recall, archives candidates, marks confirmed generated observations stale,
and preserves the additive tables and audit history.
