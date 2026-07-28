# MemoryMaster vNext governed capture audit delta
# Covers: SQLite activation, capture, lineage, trusted graph, scheduling, and public v1 evidence.
# Key terms: memorymaster.public.v1, capture_jobs, legacy event DAG, hidden tasks, candidate writes.
# Read when: reviewing the activated local candidate or deciding whether the 24-hour PR gate passes.
# Baseline: d33a268; activated branch includes 8502933 through 41ae21d.
# Evidence boundary: local SQLite and private providers; Postgres was explicitly waived for this release.
# Verdict: locally activated with rollback evidence; PR, seven-day gate, and public publication remain open.

## Scope and implementation

This delta covers only the vNext surfaces introduced by governed universal
capture and the activation defects found while exercising them. It does not
reopen findings outside capture, lineage, graph integrity, scheduled
processing, or the public facade.

| Package | Commit | Result |
| --- | --- | --- |
| Baseline, roadmap, ADR | `8502933` | Product boundaries, current metrics, and governed lineage decision recorded. |
| Durable lineage and queue | `e85d10f` | Additive migration, exact backfill, replay-safe jobs, and support rows implemented. |
| Universal adapters | `27372fc` | Text, Markdown, HTML, optional PDF/DOCX, and explicit media-provider capture implemented with root and size guards. |
| Public facade and trusted graph | `dc40c01` | Python, CLI, MCP, producers, background worker, personal-v1 ontology, and support-authorized graph traversal implemented. |
| Capture Inbox and demo | `c5e1b3d` | Local dashboard lineage view, retirement preview/apply, public onboarding, and disposable deterministic demo implemented. |
| Convergence | `f9350c5` | Sensitive inbox masking, browser verification, package checks, and release evidence completed. |
| Legacy migration compatibility | `0b61565` | Exact v9 adoption and migration 18 preserve a verifiable legacy event DAG without rehashing history. |
| Windows provider timeout | `a391784` | Timed-out OpenCode calls terminate their complete Windows process tree. |
| Dreaming crash recovery | `945bdc2` | A newly acquired lease reconciles historical `running` rows as explicitly abandoned. |
| Steward duplicate integrity | `41ae21d` | Later duplicates archive against the canonical claim without overwriting its one reciprocal supersession link. |

Postgres implementation remains additive in the branch for source
compatibility, but the operator selected SQLite-only activation and explicitly
waived live Postgres parity for this release. No Postgres server or DSN was
used.

## Storage activation and rollback evidence

The active SQLite database was stopped at the scheduler boundary before each
quiesced operation. Original task definitions were exported before
replacement.

| Evidence | Result |
| --- | --- |
| Initial rollback snapshot | `memorymaster-pre-vnext.db`, 6,090,358,784 bytes, SHA-256 `06bce2bf57e7ab584d72eed377e6e0cac1219158e3c27358316d7fca2f6f852a` |
| Immediate pre-activation snapshot | `memorymaster-pre-activation-final.db`, 6,095,273,984 bytes, SHA-256 `686c2fbb07b0a8a5a4a3101d589389345ea7d0b6817c554d59c61e5641bde253` |
| Backup root | Configured `E:\MemoryMaster\activation\20260727-vnext` location |
| Restore proof | Byte-matching disposable restore, `quick_check=ok`, zero foreign-key rows, and matching core counts |
| Fresh migration proof | Original snapshot migrated through version 18 in 37.76 s; replay completed in 11.1 ms |
| Live migration proof | First run completed in 37.09 s; replay completed in 18.85 ms; core row counts remained unchanged |
| Previous-package read proof | Previous package queried the migrated restore successfully with 12 results |
| Rollback compatibility | Additive tables remain in place for the previous package to ignore; original task XML is retained |

The legacy event ledger contained 1,622,356 surviving rows at activation: all
stored event content hashes verified, with 330 historical fork points and 66
missing predecessor hashes. Migration 18 preserved these signed facts exactly,
reported the topology, rebuilt tenant metadata, and restored append-only
triggers. It did not rewrite history into a false linear chain.

## Verification evidence

### Functional, storage, and lifecycle

- Final exact-SHA non-ML suite: 4,198 passed, 71 skipped, 95 deselected,
  one expected xfail, and one existing Pydantic deprecation warning in
  952.53 seconds.
- Activation hardening added focused coverage for Windows process-tree
  termination, stale Dreaming run reconciliation, and repeated validator
  duplicates.
- Final ML-marked suite: 95 passed, 4,270 deselected, with the same existing
  Pydantic warning.
- Migration and restored/live databases passed `quick_check`; foreign-key
  checks returned no rows.
- Exact replay produced no duplicate source, evidence, capture-job, claim, or
  edge-support records in disposable tests.
- Trusted live `memorymaster.public.v1` recall returned only `confirmed`
  lifecycle rows and preserved citations after Dreaming and Steward cycles.
- The active capture tables remain empty because no new public capture was
  submitted during activation; no capture job was orphaned.

### Retrieval, capture quality, and headless evaluation

The reproducible 500-question LongMemEval candidate run produced R@5 0.9660,
R@10 0.9840, and MRR 0.9033912698 versus baseline R@5 0.9660, R@10 0.9840,
and MRR 0.9020579365. The retrieval regression gate passes.

One bounded private Codex headless evaluation used `gpt-5.6-sol` at high
reasoning, with no subagents or retry fan-out. Three synthetic private cases
produced 100 percent candidate, entity, and relationship precision. Usage was
28,606 input tokens, 893 output tokens, and 516 reasoning tokens. This is a
small activation smoke, not a statistically sufficient substitute for the
90/90/85 private-corpus gate.

The comparable full OAuth-backed before/after QA judge was not run. The
existing one-question smoke proves only judge-path readiness, not answer
quality. Full QA remains a release-quality gate, not an activation invariant.

### Package and supply chain

- The exact live candidate wheel was rebuilt after activation fixes and
  installed into the pinned private runtime.
- Final live wheel SHA-256:
  `b7476e18a76ce3e4e4c101466090fa7ce6946981e4ffbb2f418734786fd77a18`.
- Default and `capture`-extra clean-wheel installations, package-content
  inspection, SBOM generation, and dependency audit passed before activation.
- Gitleaks reported 71 known whole-tree fixture/index/cache findings and zero
  findings in the exact implementation delta checked before activation.

## Live scheduler and candidate-write evidence

Both task actions now execute the pinned runtime through `pythonw.exe`; no
PowerShell, Git Bash, or console window is part of either action. The original
triggers, principals, settings, and task XML backups were preserved.

`setup-hooks --verify-only --json` passed its disposable sentinel and reported:

- both tasks configured, hidden, and mode-matched;
- empty public capture-job queue;
- Dreaming extractor and consolidator discoverable;
- direct capture claim extractor, OCR, and transcription providers not ready.

The first authorized Dreaming cycle:

- honored the 15-minute stale lease rather than stealing it;
- removed each timed-out OpenCode process tree at 180 seconds;
- left no Dreaming or OpenCode process and released its lease;
- reconciled seven historical phantom runs as `abandoned`;
- applied 20 previously consolidated captures;
- wrote 34 candidate claims and one review proposal;
- finished `partial` with four recorded provider errors.

Dreaming Task Scheduler result remains `1`, correctly reflecting the Gemini
quota and OpenCode timeout failures. The sources and queued work were preserved
for safe retry.

The next hourly run started from the installed trigger without manual
intervention. It applied 20 more consolidated captures, wrote 24 candidates
and seven proposals, timed out and removed both OpenCode process trees, left
no lease or child process, and again returned the truthful result `1`.

The final Steward cycle:

- completed with Task Scheduler result `0` and no new `steward error`;
- processed 2,000 candidates through the existing governed pipeline;
- confirmed eight claims through the canonical validator;
- archived one repeated duplicate against its canonical claim;
- completed `quick_check=ok` with zero foreign-key orphans;
- left trusted recall confirmed-only.

The existing Steward cadence policy also staled and archived older unused
claims and created its configured E-drive vacuum snapshot. Those pre-existing
governance behaviors were not introduced or expanded by vNext.

## Open gates and operator actions

1. Observe the hourly Dreaming task and Steward telemetry for 24 hours from
   the completed first automatic cycle at `2026-07-28T06:17:18Z`.
2. Do not create the PR unless the 24-hour check confirms no orphan process or
   lease, no duplicate writes, acceptable queue depth, preserved trusted
   recall, and an explicit disposition for Dreaming's provider failures.
3. Earliest PR decision time is `2026-07-29T06:17:18Z`
   (`2026-07-29 03:17:18` America/Argentina/Buenos_Aires).
4. Continue the seven-day observation for capture precision, graph support,
   provider usage, task results, and recall regressions.
5. Keep full comparable OAuth QA and the statistically sufficient private
   capture/graph precision corpus open as release-quality evidence.
6. Public publication, package publication, and public deployment remain
   prohibited until separately authorized.

## Rollback position

Rollback remains additive and conservative: disable capture/ontology worker
flags, restore the prior task actions from the saved XML, and leave new tables
in place for the older package to ignore. Restore
`memorymaster-pre-activation-final.db` only if an invariant or migration
failure affected existing rows. No rollback trigger has fired: the live
database passes integrity and compatibility checks.
