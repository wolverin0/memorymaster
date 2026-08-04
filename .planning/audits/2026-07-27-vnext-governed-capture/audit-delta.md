# MemoryMaster vNext governed capture audit delta
# Covers: SQLite activation, capture, lineage, trusted graph, scheduling, and public-safe v1 evidence.
# Key terms: memorymaster.public.v1, OpenCode OAuth, provider window, candidate writes, observation.
# Read when: reviewing the local candidate, public PR readiness, or remaining release-quality gates.
# Baseline: d33a268; pre-retirement code head is 6be23d7.
# Evidence boundary: SQLite/private providers; private paths, fingerprints, and row counts are excluded.
# Verdict: 24-hour, LifeAgent, Docker, and full-QA gates passed; no public push or publication is included.

## Scope and implementation

This delta covers only the vNext surfaces introduced by governed universal
capture and the activation defects found while exercising them. It does not
reopen findings outside capture, lineage, graph integrity, scheduled
processing, or the public facade.

## Post-observation LifeAgent retirement (2026-07-31)

The prior cumulative provider result is superseded as an observation signal: it
retained pre-fix failures and did not represent the bounded final-code window.
The replacement 24-hour observation passed above the required structured-yield
threshold; every post-anchor provider call was structured-valid and completed
without HTTP 429 or provider error outcomes. The public status contract now
labels its backward-compatible lifetime aggregate separately from a rolling
24-hour provider window, and low-yield warnings are calculated from that window.

The retirement write was gated by a SQLite backup-API snapshot and a disposable,
byte-matching restore. The restore passed `quick_check` and foreign-key checks.
Backup locations, database fingerprints, sizes, and personal row counts remain
in local operator evidence and are intentionally excluded from this repository.

All non-archived exact-scope `project:lifeagent` claims were retired through the
public `memorymaster.public.v1` `forget(..., apply=True)` operation. No source,
evidence, claim, citation, or event was hard-deleted; repeat retirement was
idempotent and preserved evidence. Trusted recall now returns no LifeAgent
claims. The active database passed post-write integrity and foreign-key checks,
candidate citations remained intact, duplicate idempotency checks were clear,
and the capture queue and leases were empty. Focused lifecycle coverage, Ruff,
and `git diff --check` passed.

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
| Initial rollback snapshot | Verified by SQLite backup API, full read, and cryptographic identity; private artifact details retained locally. |
| Immediate pre-activation snapshot | Verified independently before writes; private artifact details retained locally. |
| Backup root | Operator-configured location outside the repository. |
| Restore proof | Byte-matching disposable restore, `quick_check=ok`, zero foreign-key rows, and matching core counts |
| Fresh migration proof | Original snapshot migrated through version 18 in 37.76 s; replay completed in 11.1 ms |
| Live migration proof | First run completed in 37.09 s; replay completed in 18.85 ms; core row counts remained unchanged |
| Previous-package read proof | Previous package queried the migrated restore successfully with governed results. |
| Rollback compatibility | Additive tables remain in place for the previous package to ignore; original task XML is retained |

All stored content hashes in the legacy event ledger verified at activation.
Migration 18 preserved the existing historical fork and predecessor topology,
rebuilt tenant metadata, and restored append-only triggers. Exact private-ledger
counts remain in local evidence; the migration did not rewrite history into a
false linear chain.

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
- The 2026-08-01 stabilization rerun passed 98 focused Dreaming, surface,
  release-truth, setup-profile, and supply-chain contract tests. Ruff passed,
  and collection completed with 4,380 tests.
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

Comparable full OAuth-backed before/after QA passed with the same keyless
OpenCode 1.18.13 judge (`openai/gpt-5.4-mini`, medium effort). Baseline accuracy
was 46.2 percent (231/500) and retained accuracy was 46.4 percent (232/500), a
+0.2 percentage-point delta versus the permitted -2.0-point floor. This closes
the answer-quality comparison without changing the activation invariant.

### Package and supply chain

- The exact live candidate wheel was rebuilt after activation fixes and
  installed into the pinned private runtime.
- Final live wheel SHA-256:
  `b7476e18a76ce3e4e4c101466090fa7ce6946981e4ffbb2f418734786fd77a18`.
- Default and `capture`-extra clean-wheel installations, package-content
  inspection, SBOM generation, and dependency audit passed before activation.
- The final-code verification wheel installed cleanly with both default and
  `capture` extras; strict OSV audits covered 8 and 12 installed dependencies
  respectively and reported zero known vulnerabilities.
- Its CycloneDX 1.6 SBOM validated against the exact wheel identity and hash.
- On 2026-08-01, the current worktree rebuilt as a wheel and source archive;
  Twine metadata, package-content allowlisting, the default clean install, and
  the `capture`-extra clean install all passed.
- Gitleaks 8.21.2 strict full-history and uncommitted-patch scans reported zero
  unreviewed findings. Synthetic redaction fixtures remain admitted only by
  exact commit/path/rule/line fingerprints.

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

The decisive live run completed with Task Scheduler result `0`:

- captures consolidated or deferred cleanly at the existing daily model budget;
- zero errors, retryable rows, leases, hook errors, or OpenAI HTTP 429s;
- no duplicate or orphaned work was observed.

That run started an observation but did not complete it. The next automatic
cycle exposed a 75 percent call-level structured yield from GPT-5.4 Mini.
Reproduction on the same sanitized queued envelopes found an OpenCode exit-1,
malformed JSON, and noncontiguous evidence quotes. The observation was
therefore invalidated instead of being counted toward the PR gate.

GPT-5.6 Terra at medium effort eliminated Mini's transport failures but raw
quote copying still produced only 65 percent fully valid calls in a bounded
live cycle. Higher Terra effort and GPT-5.6 Sol at low effort were worse on the
bounded no-write sample, so the runtime did not escalate to a larger model.

The durable correction removes verbatim copying from the LLM contract.
Sanitized messages are deterministically divided into exact source spans; the
model selects a supplied span ID, and MemoryMaster resolves the stored message
ID and quote from the original sanitized envelope. Unknown span IDs fail
closed. A subsequent bounded live run improved to 90 percent fully valid calls
and isolated the remaining rejection to project-specific knowledge
labeled personal. A one-way repair now routes only candidates with
unmistakable project markers back to the capture's project scope. Ambiguous
personal facts remain rejected.

The final bounded no-write regression produced provider success, fully valid
results, accepted candidates, and zero rejection codes for every eligible
capture in the sample. The exact final code is installed at `da0b9f9` with
Terra Medium extraction and Luna Low consolidation. The active daily Terra
budget was not bypassed; the authoritative replacement cycle began after the
normal UTC budget reset.

The first hourly final-code cycle deferred eligible captures at the existing
Terra daily limit, returned zero errors, released its lease, and completed with
Task Scheduler result `0` and no missed runs. The corresponding pre-window
integrity baseline confirmed citations and idempotency keys on recent Dreaming
candidates, zero duplicate capture/application key groups, no active leases,
and no pending capture error. Exact queue and candidate counts remain in local
operator evidence.

Earlier live failures were preserved during diagnosis. Their final root cause
was not OAuth: Luna emitted correct decisions for the supplied candidates and
then incorrectly emitted additional decisions for reference claim IDs. The
stabilized prompt makes current claims reference-only, supplies an explicit
candidate-ID allowlist and decision count, and caps batches at five. The exact
previously failing five-candidate fixture then passed in no-write mode with an
exact decision-ID set before live activation.

The final Steward cycle:

- completed with Task Scheduler result `0` and no new `steward error`;
- processed candidates through the existing governed pipeline;
- confirmed eligible claims only through the canonical validator;
- archived repeated duplicates against their canonical claims;
- completed `quick_check=ok` with zero foreign-key orphans;
- left trusted recall confirmed-only.

The existing Steward cadence policy also staled and archived older unused
claims and created its configured off-repository vacuum snapshot. Those
pre-existing governance behaviors were not introduced or expanded by vNext.

## Completed activation gates and remaining release evidence

The final-code provider run and replacement 24-hour observation are complete.
They passed the structured-yield threshold, scheduler/lease checks, duplicate
checks, queue-health checks, and confirmed-only trusted recall checks. The
LifeAgent scope retirement, verified restore, and idempotent repeat are also
complete. `dream-status` now distinguishes the rolling provider window from
lifetime history, so historical failures no longer produce a misleading
current-window warning.

The remaining work is release evidence, not an activation blocker:

1. Continue the seven-day observation for capture precision, graph support,
   provider usage, task results, and recall regressions.
2. Expand the private capture/graph corpus enough to make the 90/90/85 quality
   thresholds statistically meaningful.
3. Obtain separate operator authorization before creating a public PR, pushing
   this branch, publishing a package, or performing a public deployment. This
   stabilization does none of those actions.

## Rollback position

Rollback remains additive and conservative: disable capture/ontology worker
flags, restore the prior task actions from the saved XML, and leave new tables
in place for the older package to ignore. Restore the verified pre-activation
snapshot only if an invariant or migration failure affected existing rows. No
rollback trigger has fired: the live database passes integrity and
compatibility checks.
