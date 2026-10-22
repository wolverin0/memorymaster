<!-- doc-head: AO-3 configuration, incremental source cursor and local acceptance -->
<!-- Covers: default-off gates, pilot limits, incremental reads and dated source verification. -->
<!-- Read when: reviewing or later authorizing the AO-3 pilot; no installation is implied. -->
<!-- /doc-head -->
# Project projections

The deterministic project reader serves confirmed cited decisions, constraints,
purpose and gotchas, plus an authorized filtered ledger export. SQLite remains
authoritative. Cards and generated Markdown never become claim evidence.

The implementation follows the complete AO-3 design in
`artifacts/inputs/2026-10-22-AO-3-PROJECTION-DESIGN-v3.md` and its round-two
review. It changes package source and hook templates only. Installation,
external Cockpit integration, activation and the independent real three-repository
M2 probe remain separate owner actions. Fixture success is not installed M2
acceptance. Original operator notes and GitNexus symbols remain v1.1 work.

## Configuration

All five switches default off. A child switch cannot bypass the master gate:

| Environment variable | Purpose |
|---|---|
| `MEMORYMASTER_PROJECT_PROJECTION_ENABLED` | Master build, serve and state gate |
| `MEMORYMASTER_PROJECT_PROJECTION_REFRESH` | Separate deterministic beat phase |
| `MEMORYMASTER_PROJECT_PROJECTION_SESSION_START` | Shared project hook delivery |
| `MEMORYMASTER_PROJECT_SESSION_DELTA` | Start baselines and explicit end receipts |
| `MEMORYMASTER_PROJECT_PROJECTION_COCKPIT` | Reserved external consumer activation |

`MEMORYMASTER_PROJECT_PROJECTION_PROJECTS` defaults to an empty JSON list.
Each of at most three mappings supplies `scope`, `context_key`, `project_root`,
`db_path`, explicit `tenant_id` (JSON null means only the unset legacy tenant),
optional `aliases`, `ledger_repo` and `ledger_filtered: true`. The latter asserts
that the lane supplied an authorized visibility-filtered export. Blind/private
cards are still excluded. Unknown mapping or unavailable/malformed ledger data
cannot mean zero open cards.

Approved absolute roots are supplied through
`MEMORYMASTER_PROJECT_PROJECTION_DIR`, `MEMORYMASTER_PROJECT_SESSION_STATE_DIR`
and `MEMORYMASTER_PROJECT_LEDGER_TASKS_DIR`. There are no home-directory output
defaults. `MEMORYMASTER_PROJECT_PROJECTION_EPOCH` is a required fresh activation
token; retired-epoch cache and continuity state are ignored. Context keys and
epochs are bounded opaque names, not paths. The repository owner, strict
transcript resolver and generic scope resolver must agree before hook delivery.

Manual commands branch before service construction or migrations:

```text
python -m memorymaster.profile project-build --help
python -m memorymaster.profile project-read --help
```

Both require explicit database, scope, tenant, context, epoch, repository,
projection and state roots. Use `--tenant legacy-null` only for NULL tenancy.
Ledger arguments identify the filtered input. The master flag is still required.
These commands do not enable automatic refresh or consumers.

## Integrity and budgets

`project-<slug>.md` and its JSON manifest live below the explicit context root.
One kernel lock serializes publishers, unique temporary files are flushed and
replaced, and readers verify the pair, generation, byte counts, item spans and
hashes. A torn pair is unavailable. Rehydration uses a read-only snapshot, exact
tenant/scope checks and bounded primary-key support reads. A changed claim
withholds only its dependent bullets. A changed still-open card is rendered
from its current authorized record, without a false trimmed-card warning.

The complete projection file, including its marker pair, is at most 2,200 UTF-8
bytes. The delta is at most 500 bytes including labels, references and counts.
SessionStart wraps the marker-free shared payload, delta and fitting complete
profile lines in one marker pair and checks the final 3,000-character ceiling.
It never slices a validated project payload. Cards and the latest three
decisions reserve space before optional prose. Mandatory omissions visibly
block M2 readiness. The public manifest excludes protected source coverage.

Builder snapshots cap all watched project rows at 512, citations at 2,048,
cards at 256, configured ledger records at 1,024 and source bytes at 8 MiB.
The all-status watch cap is deliberately stricter than counting eligible
claims: candidates and unsupported rows may later become eligible. An exceeded
cap is partial. Reader support limits are 64 claims, 256 citations and 2 MiB
shared source bytes, with a 25 ms SQLite busy timeout and cooperative 100 ms
SQL deadline. OS filesystem stalls are not hard real-time bounded.

## Continuity and cheap refresh

Session baselines contain fingerprints and an internal bounded scan fence,
never source prose or raw session tokens. The fence watches existing source
IDs, including ineligible rows, and the greatest observed ID. Incremental starts
rehydrate those IDs plus inclusively changed rows and IDs above the fence.
This catches same-second writes, a previously unsupported claim gaining a
citation, and old-timestamp writes committed after the prior read. Store
AUTOINCREMENT and SQLite's single writer underpin the new-ID fence. Incomplete
or older fence-less metadata is partial, not a false no-change result.

Delta selection unions differences from the selected prior start baseline and
the active projection's coverage. End events keep the start snapshot; they do
not acknowledge changes at end time. Missing projection coverage still permits
the prior-baseline comparison, labeled partial. After twelve hours an unclosed
active start can supply the newest stale baseline, explicitly labeled
`prior_session_unclosed`; inactivity never fabricates an end receipt.

One atomic `sessions.json` per context/project retains at most sixteen active
starts, four completed starts and thirty-six deduplicated baseline blobs, each
at most 128 KiB, in a file capped at 8 MiB. Starts collect sources outside the
state lock and validate the sequence when reacquiring it. Duplicate starts
preserve their baseline. Replayed compacted ends, future ends, malformed state,
capacity exhaustion and lock/write failures cannot advance the latest-end
pointer. End recording precedes optional capture or distillation. Library
`run` never invents lifecycle receipts; the CLI requires both `--session-id`
and `--end-at` to record an explicit observed end.

Refresh is separately gated and runs on enabled beat outcomes including SKIP,
capture-only and worker-busy. It checks SQLite data version, database/WAL and
ledger metadata before scanning; a bounded manifest hint survives scheduler
process restarts. Temporal boundaries defeat the cheap cache. An unchanged
SKIP does not rewrite Markdown or `checked_at`; successful non-SKIP validation
may update manifest freshness without rewriting Markdown. Changes rebuild.
Source failure or budget/lock refusal leaves the successful watermark intact.
Refresh rotates at most three pilot contexts under a cooperative 750 ms tick
budget. A validation older than sixty minutes is withheld pending refresh.

## Activation and rollback

Activation requires the lane's Argon source review and specific installation
and consumer-change authority. Install package/templates with every switch off,
configure approved contexts/roots and a new epoch, inspect manual builds first,
then separately authorize consumers, real M2 and automatic refresh. Existing
capture, Dreaming application and user-profile approvals remain independent.
The operational check reports cache/readiness and bounded receipt health. It
cannot substitute a healthy cache for a real independent installed M2 receipt.

For rollback:

1. Remove the affected allowlist mapping (or all pilot mappings). Set refresh,
   SessionStart, delta and Cockpit switches to `0`, then set the master to `0`
   in each affected environment. Record the retired epoch and sanitized reason
   on the existing build card.
2. Reload or restart only affected long-lived consumers/jobs as separately
   authorized. Fresh hooks read the changed environment on their next call.
   Start a fresh conversation to verify rollback; previously injected context
   cannot be retracted from an active conversation.
3. Verify the fresh hook uses its legacy branch, external Cockpit hides its
   generated section, beat does no projection source/cache work and SessionEnd
   writes no project continuity state. Disabled gates return before cache or
   source reads. Do not disable unrelated capture/profile/beat features.
4. Retain derived files for inspection. Do not delete claims, restore a database
   or ingest generated output. Keep the shipped span guard enforced. A later
   activation uses a new epoch and fresh baselines.

## Local verification record

Red-first checks demonstrated missing modules and the new behavioral failures
before implementation: raw generated-span intake/dedup and HTML capture;
full-turn transcript filtering; hook lifecycle ordering; per-bullet card edits;
reserved UTF-8 budgets; malformed/out-of-order session state; missing projection
continuity; unresolved scope refusal; scheduler restart hints; same-second and
late committed writes; first citations; and equal-stat ledger rewrites.

The explicit ten-thousand-session stress test passed with bounded deduplicated
state. A fixture M2 exercise uses three disposable repositories and template
hooks with the shared reader, with no independent SQL/model fallback. External
Cockpit and the real installed probe are not represented as completed.

Windows sandbox directory creation required a test-launcher-only wrapper that
uses mode `0777` for disposable pytest directories. Production code and shared
pytest fixtures were not changed for that issue. The first broad service
coverage attempt was interrupted during provider initialization; affected
ingest cases passed with explicit hash embeddings. The first non-ML attempt
was interrupted before failures to give the ten-thousand-session stress test
its explicit 180-second timeout; it had no test failures at interruption.
Final suite counts and commands are recorded in the task close report.

### Resumed source checks, 2026-10-22

At the end of the previous resume, source acceptance was **partial** pending the round-two incremental-read
criterion. `load_project_snapshot` unions every watched claim ID into the
SessionStart lookup; `_read_claims` then reloads those rows and their citations.
The query is bounded and uses primary-key lookups, but does not reuse unchanged
coverage without materializing its sources. A disposable unchanged-source
probe with three watched claims observed three rehydrated rows and three
rehydrated citations. `test_session_start_baseline_never_full_source_scan`
checks the query arguments only and does not establish incremental cost.

Simply removing watched IDs would regress the passing citation-only,
same-second, first-citation and old-timestamp late-commit tests. A bounded SQL
fingerprint comparison could reduce Python materialization while still reading
all watched source fingerprints. Strict change-only reads need a trustworthy
mutation sequence or another approved design. The lane's Argon review must
resolve that difference before claiming the incremental criterion or activating
the pilot; no journal, migration or activation was added in this resume.

The resume also found and fixed pinned decisions incorrectly sorting behind
older unpinned decisions. The new
`test_latest_decisions_ignore_pin_priority` first failed with observed IDs
`[11, 10, 9]` instead of `[14, 13, 12]`; the complete projection file then passed
all twelve tests. Pins still order constraints, while decisions use date and ID.

Freshly executed checks, one test file per Python process:

| Test file (`tests/`) | Passed | Skipped |
|---|---:|---:|
| `test_project_projection.py` | 12 | 0 |
| `test_project_repository.py` | 22 | 0 |
| `test_project_sessions.py` | 13 | 0 |
| `test_project_runtime.py` | 8 | 0 |
| `test_project_integration.py` | 5 | 0 |
| `test_generated_artifacts.py` | 3 | 0 |
| `test_compiled_profile_surfaces.py` | 6 | 0 |
| `test_profile_config_defaults.py` | 2 | 0 |
| `test_compiled_profile.py` | 9 | 0 |
| `test_compiled_profile_claim_supports.py` | 50 | 0 |
| `test_intake_policy.py` | 22 | 0 |
| `test_service.py` | 6 | 0 |
| `test_capture_adapters.py` | 24 | 1 |
| `test_public_v1.py` | 14 | 0 |
| `test_dreaming_self_observation.py` | 35 | 0 |
| `test_session_end_capture_hook.py` | 2 | 0 |
| `test_session_end_ingest.py` | 4 | 0 |
| `test_scheduled_task_runtime.py` | 7 | 0 |
| `test_operational_review.py` | 16 | 0 |
| `test_public_demo.py` | 2 | 0 |
| `test_scope_utils.py` | 17 | 0 |
| **Total** | **279** | **1** |

Runs used `WEZBRIDGE_OPERATOR_ALLOW_HEAVY=1`, disposable workspace basetemps
and the local directory-mode wrapper described above. Service-backed suites
used `MEMORYMASTER_EMBEDDING_PROVIDER=hash`; default temporary-directory roots
were redirected into the workspace. The sole skip requires Windows symlink
privilege. The ten-thousand-session case passed in the complete session file
(125 seconds). No full-suite or installed/live acceptance is claimed.

The first projection attempt had eleven fixture setup errors because its
basetemp parent did not exist; the next exposed the sandbox's `0700` directory
access failure. Creating the parent and using the disposable-only wrapper
resolved those harness errors. The first standalone incremental probe imported
the installed package because its script path omitted the checkout; explicitly
prepending the checkout resolved that harness error before the observation
above. These were retained as failed attempts, not product regressions.

Ruff passed on all thirty-three changed Python files. Release truth was
regenerated for the new regression and `--check` passed. Contract witness
passed against `origin/main` (seventeen code files, sixteen test files, no
orphan witnesses). GitNexus reported `INDEX_MISSING`; deferred tool discovery
exposed neither GitNexus nor Serena. Scoped source references and the reviewed
diff covered the builder's CLI/refresh callers before committing. Branch
freshness passed with zero commits behind `origin/main`.

## Round-three incremental collection (T-1703)

The incremental source criterion now passes locally. The first red run of
`test_session_start_incremental_counts_only_changed_rows_and_citations`
failed all three cases: forty known claims caused forty claim and citation
loads even with zero, one or three changes. The corrected cases load exactly
zero, one or three claim rows and their citations, including overlapping claim
and citation edits, while retaining unchanged support hashes. Unchanged rows
from the same second are also excluded. SQL query plans assert primary-key
payload lookups and indexed citation lookups without scanning the source tables.

The previous timestamp-only gap demonstrated the need for versioned migration
29: a singleton epoch/sequence clock and one latest-change row per claim,
including deletion tombstones. Transactional triggers track claim and citation
inserts, updates, deletes and identity moves without storing source prose.
This is bounded per claim identity rather than an append-only event history.
Indexed sequence changes are unioned with indexed inclusive `updated_at` and
temporal-boundary ranges and new IDs, then deduplicated before loading payloads.
The same read-only SQLite transaction captures the clock, selected IDs, claims
and citations. Coverage carries unchanged manifest support hashes and removes
changed identities before adding currently eligible supports.

The source-load cost is O(N + R): N unique changed claims and their citations,
plus R supports needed for prior-session delta prose when a refreshed
projection already includes those changes. Lazy delta loading stops at the
500-byte output limit or the first unavailable support, with at most one
additional candidate. A forty-claim refreshed-projection regression counts
those loads against the rendered delta supports. The shared projection keeps
its 2,200-byte cap and delta entries retain exact support hashes and spans.
Coverage and bounded session-state serialization still operate on compact
metadata, and configured ledger reads retain their existing bounds.

Migration 29 is SQLite-only and PostgreSQL fails closed. Writable store
initialization installs it, including convergence after a standalone migration
runner stamped an empty database. SessionStart never initializes or migrates
the store. Missing, foreign or rewound cursors withhold the baseline without
falling back to a source scan; old projections require a fresh generation after
schema initialization. The migration and initialization checks used disposable
databases only, including backup/restore and late WAL commits. No authoritative
database or installed service was changed; activation and independent Argon/M2
acceptance remain separate lane actions.

Retained failed evidence: the first repository integration run had two failures
from tests that assumed an empty coverage could bootstrap an incremental read
and that payload SQL contained the old watched-ID union. Those witnesses now
check temporal activation/expiration with known coverage and the indexed
selection followed by a deduplicated payload lookup. Cursor tests first exposed
an untracked old primary key on identity moves and a bare-runner initialization
gap; both received failing regressions before their fixes.

Fresh round-three verification used one test file per Python process and the
same workspace-only temporary-directory launcher as the previous resume:

| Test file (`tests/`) | Passed |
|---|---:|
| `test_project_projection.py` | 16 |
| `test_project_repository.py` | 31 |
| `test_project_sessions.py` | 14 |
| `test_project_runtime.py` | 8 |
| `test_project_integration.py` | 5 |
| `test_project_source_cursor.py` | 13 |
| `test_public_demo.py` | 2 |
| `test_migrations.py` | 18 |
| `test_initdb_fastpath.py` | 6 |
| **Total** | **113** |

Ruff passed on all eight changed Python files. Release truth was regenerated
and `--check` passed. Contract witness passed against `origin/main`; source
navigation and final change-scope review used live references and Git diffs
because this checkout still has no GitNexus index or exposed navigation tools.
No push, merge, installed-hook edit or live database check belongs to this round.
