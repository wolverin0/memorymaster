# MemoryMaster vNext governed capture audit delta
# Covers: SQLite activation, capture, lineage, trusted graph, scheduling, and public v1 evidence.
# Key terms: memorymaster.public.v1, OpenCode OAuth, exact evidence spans, candidate writes, observation.
# Read when: reviewing the activated local candidate or deciding whether the 24-hour PR gate passes.
# Baseline: d33a268; current stabilization head is da0b9f9.
# Evidence boundary: SQLite/private providers; 4,210 non-ML and 96 ML tests pass; Postgres waived.
# Verdict: stabilization continues; the first observation was invalidated and the replacement has not started.

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
| OpenCode OAuth extraction | `4a9e95a` | OAuth-only GPT-5.4 Mini extraction, model-specific stage budgets, and provider-aware readiness replaced the exhausted Gemini free-tier path. |
| Budget deferral | `d083abf` | Consolidation budget exhaustion now preserves extracted work without retry/error churn or task failure. |
| Candidate-ID integrity | `df06d99` | Five-ID batches and a reference-only current-claims contract prevent claim IDs from entering candidate decisions. |
| Time-stable calibration gate | `af6deeb` | The CLI calibration fixture no longer drifts outside its 90-day window at UTC date boundaries. |
| Extraction prompt contract | `ebdc5cc` | OpenCode extraction instructions now mirror the fail-closed candidate validator. |
| Exact evidence spans | `612aa6d` | Models select deterministic sanitized source-span IDs instead of copying or joining evidence text. |
| Project-scope repair | `da0b9f9` | Unmistakably project-specific candidates mislabeled personal route one-way back to project; ambiguous personal facts still fail closed. |

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

- Final exact-span/scope-head non-ML suite: 4,210 passed, 71 skipped, 96 deselected,
  one expected xfail, and one existing Pydantic deprecation warning in
  665.94 seconds.
- Activation hardening added focused coverage for Windows process-tree
  termination, stale Dreaming run reconciliation, and repeated validator
  duplicates.
- Final ML-marked suite: 96 passed, 4,282 deselected, with the same existing
  Pydantic warning, in 375.60 seconds.
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

Stabilization reproduced the Gemini failure as a free-tier limit of 20
requests per project/model/day, then proved OpenCode OAuth with
`OPENAI_API_KEY` removed from both child stages. The initial GPT-5.4 Mini
extraction configuration was subsequently replaced by GPT-5.6 Terra at medium
effort after live yield failures; GPT-5.6 Luna at low effort continues to
perform lifecycle consolidation.

The decisive live run `dream-28f695a354c241fe9e508b36548bf993` completed at
`2026-07-30T00:58:53.996749Z` with Task Scheduler result `0`:

- one capture consolidated and applied, with five candidate writes;
- four captures cleanly deferred at the existing daily model budget;
- zero errors, retryable rows, leases, hook errors, or OpenAI HTTP 429s;
- queue state `applied=161`, `captured=73`, `extracted=29`.

That run started an observation but did not complete it. The next automatic
cycle exposed a 75 percent call-level structured yield from GPT-5.4 Mini.
Reproduction on the same sanitized queued envelopes found an OpenCode exit-1,
malformed JSON, and noncontiguous evidence quotes. The observation was
therefore invalidated instead of being counted toward the PR gate.

GPT-5.6 Terra at medium effort eliminated Mini's transport failures but raw
quote copying still produced only 65 percent fully valid calls in a 20-capture
live cycle. Higher Terra effort and GPT-5.6 Sol at low effort were worse on the
bounded no-write sample, so the runtime did not escalate to a larger model.

The durable correction removes verbatim copying from the LLM contract.
Sanitized messages are deterministically divided into exact source spans; the
model selects a supplied span ID, and MemoryMaster resolves the stored message
ID and quote from the original sanitized envelope. Unknown span IDs fail
closed. A subsequent 20-capture live run improved to 90 percent fully valid
calls and isolated the remaining rejection to project-specific knowledge
labeled personal. A one-way repair now routes only candidates with
unmistakable project markers back to the capture's project scope. Ambiguous
personal facts remain rejected.

The final bounded no-write regression on the next ten eligible captures
produced 10 provider successes, 10 fully valid results, 47 accepted candidates,
and zero rejection codes. The exact final code is installed at `da0b9f9` with
Terra Medium extraction and Luna Low consolidation. The active daily Terra
budget was not bypassed, so an authoritative final live extraction cycle must
wait for the UTC budget reset before a new 24-hour window can begin.

Earlier live failures were preserved during diagnosis. Their final root cause
was not OAuth: Luna emitted correct decisions for the supplied candidates and
then incorrectly emitted additional decisions for reference claim IDs. The
stabilized prompt makes current claims reference-only, supplies an explicit
candidate-ID allowlist and decision count, and caps batches at five. The exact
previously failing five-candidate fixture then passed in no-write mode with an
exact decision-ID set before live activation.

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

1. Run the first final-code live extraction cycle after the UTC provider-budget
   reset and require at least 95 percent structured yield with no provider or
   worker errors.
2. Start a replacement 24-hour observation only from that green final-code
   cycle; the prior `2026-07-30T00:58:53.996749Z` window is invalid.
3. Do not create the PR unless the 24-hour check confirms no orphan process or
   lease, no duplicate writes, acceptable queue depth, preserved trusted
   recall, and an explicit disposition for Dreaming's provider failures.
4. The PR decision time is the replacement green-cycle completion plus 24
   hours; it is not yet established.
5. The configured GitHub origin is public. Do not push the branch or create a
   PR until the operator supplies an approved private target or changes the
   repository visibility.
6. Continue the seven-day observation for capture precision, graph support,
   provider usage, task results, and recall regressions.
7. Keep full comparable OAuth QA and the statistically sufficient private
   capture/graph precision corpus open as release-quality evidence.
8. Public publication, package publication, and public deployment remain
   prohibited until separately authorized.

## Rollback position

Rollback remains additive and conservative: disable capture/ontology worker
flags, restore the prior task actions from the saved XML, and leave new tables
in place for the older package to ignore. Restore
`memorymaster-pre-activation-final.db` only if an invariant or migration
failure affected existing rows. No rollback trigger has fired: the live
database passes integrity and compatibility checks.
