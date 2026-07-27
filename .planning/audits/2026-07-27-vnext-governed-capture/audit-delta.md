# MemoryMaster vNext governed capture audit delta
# Covers: capture, lineage, trusted graph, scheduling, and public-interface release evidence only.
# Key terms: memorymaster.public.v1, capture_jobs, claim_evidence_links, entity_edge_supports.
# Read when: deciding local release-candidate readiness or planning controlled activation.
# Baseline: d33a268; implementation commits 8502933 through c5e1b3d plus final convergence.
# Evidence boundary: temporary databases and local browser only; no live migration or scheduler change.
# Verdict: ready for local release-candidate review, not authorized for activation or publication.

## Scope and implementation

This delta audits only the vNext surfaces introduced by the governed universal
capture program. It does not reopen historical findings outside capture,
lineage, graph integrity, scheduled processing, or the public facade.

| Package | Commit | Result |
| --- | --- | --- |
| Baseline, roadmap, ADR | `8502933` | Product boundaries, current metrics, and governed lineage decision recorded. |
| Durable lineage and queue | `e85d10f` | Additive SQLite/Postgres migration, exact backfill, replay-safe jobs, and support rows implemented. |
| Universal adapters | `27372fc` | Text, Markdown, HTML, optional PDF/DOCX, and explicit media-provider capture implemented with root and size guards. |
| Public facade and trusted graph | `dc40c01` | Python, CLI, MCP, producers, background worker, personal-v1 ontology, and support-authorized graph traversal implemented. |
| Capture Inbox and demo | `c5e1b3d` | Local dashboard lineage view, retirement preview/apply, public onboarding, and disposable deterministic demo implemented. |
| Convergence | final commit | Sensitive inbox masking, clean browser console, checkout-local setup verification, and release evidence. |

## Verification evidence

### Functional, storage, and lifecycle

- Focused capture/facade/worker/graph verification before convergence: 153
  tests passed.
- Capture Inbox, demo, and release-truth verification: 14 tests passed after
  convergence hardening.
- Full non-ML suite: 4,191 passed, 71 skipped, 95 ML tests deselected, one
  expected xfail, and one existing Pydantic deprecation warning in 963.64 s.
- Final collection check: 4,358 tests.
- Migration, idempotence, foreign-key, disposable restore, and snapshot
  verification: 30 tests passed.
- Focused adversarial coverage passed for exact replay deduplication, changed
  content hashes, expired leases and capped retry, malformed/oversized/fake
  documents, traversal and symlink escapes, secret-bearing inputs, missing
  providers, multi-source/source retirement semantics, direct claim archival,
  cross-scope graph authorization, and replay-safe edge support.
- Accepted jobs are asserted to settle as completed, retryable, blocked, or
  cancelled; lease recovery tests prevent orphaned leased jobs.
- ML-marked suite: 95 passed, 4,263 deselected, with one existing Pydantic
  deprecation warning.
- Disposable `run-cycle` completed with `quick_check=ok`, zero foreign-key
  orphans, and no Qdrant dependency.
- Live Postgres was not exercised because no disposable test DSN was
  available. Migration SQL and mocked/store parity tests are evidence, not a
  substitute for a real Postgres run.

### Performance and retrieval

| Gate | Baseline | Candidate | Verdict |
| --- | ---: | ---: | --- |
| Perf-smoke total | 7.172 s | 6.938 s | pass |
| Ingest p95 | 0.023109 s | 0.022646 s | pass |
| Query p95 | 0.043291 s | 0.038078 s | pass |
| Cycle p95 | 2.863414 s | 2.663987 s | pass |
| Synchronous `remember` p95 | not available | 0.031013 s | pass, below 0.500 s |

The 500-question LongMemEval candidate rerun produced R@5 0.9660, R@10
0.9840, and MRR 0.9033912698 versus baseline R@5 0.9660, R@10 0.9840, and
MRR 0.9020579365. The retrieval regression gate passes. Gemini rerank calls
encountered transient 503 and quota 429 responses late in the run; the bounded
fallback preserved completion and the recorded metrics.

An OAuth-backed one-question QA smoke executed successfully but answered the
sample incorrectly. This proves judge-path readiness only. It is not the
required comparable full before/after QA run, so the full-QA regression gate
remains open.

The synthetic private capture evaluation corpus remained outside the
repository. Provider-backed candidate, entity, and relationship precision was
not measured in this run; the 90/90/85 percent precision gates remain open.

### Package and supply chain

- A clean wheel built and installed into separate default and `capture`-extra
  virtual environments.
- Candidate wheel size is 909,774 bytes, up 39,358 bytes from the 870,416-byte
  baseline; SHA-256 is
  `d4a481a8994536e55c8909c420566d5638ddb4eb61650ded480dc32df438397d`.
- The default wheel demo completed the capture, candidate, promotion, recall,
  graph, and forget-preview lifecycle and disposed its temporary database.
- Wheel inspection found 338 entries under only `memorymaster` and
  distribution metadata; tests, databases, environment files, private keys,
  and unexpected package roots were absent.
- Project dependency audit found no known runtime dependency
  vulnerabilities. Environment SBOM auditing separately flagged the clean
  virtual environment's bundled `pip 24.0`; `pip` is an installer tool, not a
  MemoryMaster runtime dependency.
- Gitleaks current-tree scan found 71 known fixture/index/cache findings and zero
  findings in files changed from `d33a268`.

### Dashboard and scheduled automation

- Real Chromium browser smoke covered the dashboard route and Capture Inbox at
  375, 768, 1024, and 1440 CSS-pixel widths.
- Responsive audit found zero high-severity failures. Mobile produced six
  medium warnings (small operator targets, clipped legacy table headings, and
  one conservative overlap) plus eight low font-size observations.
- Final browser evidence had zero console errors/warnings; all dashboard API
  calls returned 200.
- Retirement preview did not mutate state. Applying retirement to the
  disposable fixture retired the source, cancelled pending extraction, and
  preserved evidence and audit history.
- `setup-hooks --verify-only` round-tripped a disposable claim and reported
  task action, hidden execution, last result, queue depth, and provider
  readiness without changing task state.
- The installed Steward task is hidden and last succeeded. The installed
  Dreaming task is still a visible-console legacy action with a failing last
  result. The installer now generates hidden actions, but live task replacement
  was deliberately not authorized.

## Open gates and operator actions

The implementation is not activated and must not be presented as production
ready until these external or live-state gates are satisfied:

1. Run the disposable Postgres migration/parity suite against an authorized
   test DSN.
2. Run comparable full OAuth-backed QA before and after, plus provider-backed
   private capture/graph precision measurement.
3. Create and verify the configured backup snapshot of the active database,
   restore it to a disposable location, and run migration/rollback checks.
4. Under separate authorization, replace the live Dreaming and Steward task
   actions with the generated hidden wrappers and confirm real executions.
5. Observe first-cycle, 24-hour, and seven-day queue, duplicate, precision,
   provider-usage, and recall-regression telemetry.
6. Obtain separate authorization for push, publication, package installation
   over the live environment, or scheduler activation.

## Rollback position

Rollback remains additive and conservative: disable capture/ontology worker
flags, restore the prior task actions, and leave new tables in place for the
older package to ignore. Restore the active database snapshot only if an
invariant or migration failure affected existing rows.
