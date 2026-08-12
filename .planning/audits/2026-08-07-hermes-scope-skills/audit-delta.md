<!-- doc-head: Hermes P5 repair5 verifier passed; PR #189 open -->
# Hermes scope and governed-skills convergence delta
# Covers: SQLite safety, native Hermes rollout, progressive skills, rollback, and observation gates.
# Key terms: TencentDB Agent Memory, Hermes, session scope, personal-skill-v1, approved skills, snapshot.
# Read when: reviewing this feature branch, operating the activated Windows tasks, or resuming the VM rollout.
# Authority & Status: ROADMAP.md remains authoritative; repair5 passes the elapsed interval and PR #189 is open without merge authority.
# Updated: 2026-08-12 after repair5 direct verification and PR #189 creation; all broader actions remain blocked.
<!-- /doc-head -->

## Verdict

The required August 10-12 observation interval has elapsed; repair4 replays
that evidence and does not start another arbitrary 24-hour clock. Exact project
coverage, SQLite integrity, native Hermes transport, the bounded outbox, and
the installed provider now pass. The scheduled Dreaming failure came from
stale user-level variables overriding the selected Gemini+GLM pair with OpenAI,
not from an intended OpenAI dependency. The task is now pinned to Gemini Flash
Lite plus GLM 5.2 and its full scheduled replay passes; the bounded verifier
remains. P5 therefore remains open before PR creation and PPR-7 remains
unstarted. No tag, release, publication, merge, or public deployment is
authorized here.

## P5 progressive-skill evidence

- Public and MCP recall now expose additive `include_skills` and `skill_limit`
  inputs plus a structured `skills` result; defaults preserve ordinary recall.
- One shared bundle implementation serves authoritative HTTP and read-only
  replica recall. Hermes requests deterministic legacy retrieval to stay within
  its bounded provider timeout; other public callers retain the hybrid default.
- Skill projection accepts only confirmed, active, authorized skills, packs
  complete workflows into the total recall budget, and removes opaque raw skill
  JSON from the ordinary claim section.
- Focused regression: 62 passed across public v1/MCP, real authenticated MCP
  HTTP, Hermes provider/fallback, governed skill lifecycle, scope exclusion,
  stale/candidate exclusion, token budget, and replica DB-byte preservation.
- Full non-ML convergence: 4,367 passed, 71 skipped, 97 deselected, and one
  expected xfail in 684.58 seconds.
- Isolated wheel builds, package-content inspection, clean dependency install,
  imports, public signature inspection, and `pip check` pass. P5 wheel hashes:
  MemoryMaster `33D6D702DF59EDA0239CA688D3B2BF8B9C6D2101EBE8C78A76D6B24B2A8309D6`;
  Hermes provider `4694023420364BCA4BEBD200ABF2D6328426671FD432F135067F4CE2234EEED9`.

## Durable storage evidence

- Pre-P5 online snapshot:
  `E:\MemoryMaster\snapshots\memorymaster\20260808-0130-tencent-p5\memorymaster-pre-p5.db`
  (`6,684,131,328` bytes), with `PRAGMA quick_check=ok`, zero foreign-key
  violations, 125,643 claims, 128,124 citations, and 2,074,691 events. P5 has
  no schema migration, so the completed P4 restore and prior-version read gate
  remain the destructive-change rollback evidence.
- Authority snapshot:
  `E:\MemoryMaster\snapshots\memorymaster\20260807-160651-hermes-scope-skills\memorymaster-pre-activation.db`
  (`6,655,746,048` bytes).
- Restore checks were run on an independent `E:` restore and on
  `C:\Users\pauol\AppData\Local\MemoryMaster\restore-tests\20260807-hermes-scope-skills\memorymaster-restored.db`.
- Restored SQLite: `PRAGMA quick_check=ok`, zero foreign-key violations,
  repeated migration produced no changes, and 18 schema-version rows include
  migration 19.
- Snapshot and restore counts match exactly: 125,575 claims, 128,046
  citations, 2,054,173 events, one source, one evidence item, three
  claim-evidence links, one capture job, zero edge supports, and one session
  scope binding.
- The previously installed 4.5.0 runtime read the additive schema and recalled
  governed claims successfully, preserving rollback compatibility.

## Verification evidence

- Full non-ML: 4,358 passed, 71 skipped, 97 deselected, 1 xfailed.
- Required ML/retrieval: 97 passed. Collection: 4,527 tests.
- Final focused security, scope, authorization, replay, lifecycle, provider,
  governed-skill, and capture gate: 155 passed.
- LongMemEval 500: R@5 0.972, R@10 0.984, MRR 0.907565, zero provider calls,
  no regression from the controlled baseline.
- Gitleaks scanned `a0e7727..be192ff`: no leaks. Exact runtime dependency
  audit found no known vulnerabilities. Ruff, `git diff --check`, wheel
  content, and `pip check` passed.
- Final package hashes:
  - `memorymaster-4.6.0-py3-none-any.whl`:
    `B74CA275126B51584D2E74694AC0C063220B5058047C269D6AFEE0A03405A733`
  - `hermes_memorymaster-0.1.0-py3-none-any.whl`:
    `8D169DA9BCA0F98FBEBE5756046FEFDC44FA4BD559D200A44F3257C6D65FF121`

## Windows activation evidence

- The original P4 runtime remains at
  `C:\Users\pauol\.memorymaster\runtime\hermes-scope-skills-20260807`.
  P5 is installed side-by-side at
  `C:\Users\pauol\.memorymaster\runtime\hermes-scope-skills-p5-20260808`
  with MemoryMaster 4.6.0, Hermes provider 0.1.0, and a clean `pip check`.
- `MemoryMaster-Dreaming`, `MemoryMasterSteward`, and
  `MemoryMaster-MCP-HTTP-Hermes` execute the P5 runtime's `pythonw.exe`; no
  console-hosted PowerShell action was introduced. HTTP readiness is `200` and
  authenticated P5 recall returns governed context.
- Dreaming retains `--apply-candidates`. Candidate promotion remains exclusively
  controlled by the steward.
- Public `improve --scope user --max-items 10` queued the one missing graph job.
  A manual scheduled Dreaming run completed with result `0`; capture coverage
  then reported `ok`, zero missing graph jobs, zero pending graph jobs, and one
  completed graph job.
- Final verify-only status: disposable sentinel PASS, Dreaming PASS,
  candidate-apply mode matches, provider readiness true for dream, claim, and
  graph extraction, and both scheduled-task last results are `0`. The P5
  verify-only pass found the newly confirmed canary's one due graph job before
  queuing; public `improve` queued exactly that job and the 02:11 hourly worker
  completed it with result `0`. Final coverage is `ok`: seven completed jobs,
  zero missing graph jobs, and zero lease, retry, blocked, partial, or orphan
  anomalies.

## Rollback

P5 changed three Windows task actions while preserving triggers, principals,
and settings. Restore the prior runtime's `pythonw.exe` for Dreaming, Steward,
and MCP HTTP if P5 rollback is required:

- P5 rollback runtime:
  `C:\Users\pauol\.memorymaster\runtime\hermes-scope-skills-20260807\Scripts\pythonw.exe`
- VM rollback wheels:
  `~/.hermes/tmp/memorymaster-p5-20260808/rollback/`; reinstall both with
  `uv pip --no-deps --reinstall`, then restart `hermes-gateway.service`.

The older full P4 rollback actions remain:

- `MemoryMaster-Dreaming` executable:
  `C:\Users\pauol\.memorymaster\runtime\vnext-20260727\Scripts\pythonw.exe`
- `MemoryMasterSteward` executable:
  `C:\Users\pauol\.memorymaster\runtime\vnext-20260727\Scripts\pythonw.exe`

Their arguments are unchanged. Additive schema and audit/candidate rows stay in
place. Restore the database snapshot only for a proven invariant or migration
failure, not for an ordinary provider rollback.

## Live Hermes activation evidence

- The gateway outage was reproduced from the user journal: `systemd-oomd`
  killed the service cgroup after sustained user-slice memory pressure. The
  service recovered automatically. `ManagedOOMPreference=avoid` is now a
  persistent service property, leaving system OOM protection enabled while
  making the gateway a last-choice victim. Current service state is active,
  memory is approximately 300 MiB, and Telegram has established TLS sockets.
- Hermes Agent remains at `0.19.0`; the two known upstream commits do not touch
  its send, Telegram, or memory-provider paths. The working tree has unrelated
  local edits, so no live Hermes source update was attempted.
- The audited MemoryMaster `4.6.0` and native-provider `0.1.0` wheels are
  installed with `--no-deps`; the exact pre-install freeze was restored after
  an accidental resolver upgrade, and final `pip check` is clean.
- Hermes provider discovery required a literal `MemoryProvider` or
  `register_memory_provider` marker in the directory shim. The corrected shim
  is installed, `memorymaster` is selected/enabled, and the legacy lifecycle
  bridge is disabled. This prevents duplicate automatic recall/capture.
- Five authoritative shadow recalls returned confirmed claims with citations:
  median `0.492 s`, p95/max `1.373 s`. The live nonblocking prefetch return p95
  is `0.032 ms`; `sync_turn` enqueue p95 is `1.032 ms`.
- A fixed project-scoped canary created exactly one source, one evidence item,
  one completed extraction job, one candidate, and one
  `claim_evidence_links(role=support)` row. An identical replay produced no
  second delivery, source, evidence, job, claim, or link. Trusted recall did
  not return the candidate and returned only confirmed claims.
- A real Codex OAuth one-shot returned the requested sentinel. Provider outbox
  state after both canaries is zero pending, leased, retryable, and blocked.
  Direct Telegram API delivery and the gateway's persistent Telegram TLS
  connections pass. The standalone `hermes send` helper hung during a delivery
  attempt and is recorded as a separate upstream CLI defect; it is not used by
  the gateway.

## P5 live activation evidence

- New wheel hashes match the locally audited artifacts exactly: MemoryMaster
  `33D6D702DF59EDA0239CA688D3B2BF8B9C6D2101EBE8C78A76D6B24B2A8309D6` and
  Hermes provider
  `4694023420364BCA4BEBD200ABF2D6328426671FD432F135067F4CE2234EEED9`.
- Windows installed-wheel smoke proves confirmed-skill inclusion, candidate
  exclusion, token bounding, and installed-package imports. A first disposable
  run passed its assertions but hit a Windows temporary-directory cleanup lock;
  the isolated rerun exited zero, so the lock is not a product failure.
- The Hermes venv was upgraded only while the gateway was stopped, using exact
  new and rollback wheels. `uv pip check` reports 128 compatible packages. The
  current gateway has zero restarts, result `success`,
  `ManagedOOMPreference=avoid`, approximately 304 MB resident, and six
  established TLS connections. Its three post-start warnings are the expected
  Telegram DNS/connect messages and an unrelated Home Assistant filter notice;
  there is no OOM, traceback, or restart-loop warning. The intentional stop's
  prior shutdown status remains operational evidence, not a runtime failure.
- The actual provider backend is configured and returns nonempty governed
  context. Five VM-to-Windows P5 samples measured median `0.115 s` and max
  `1.045 s`. Outbox counts are completed `5` and zero pending, leased,
  retryable, blocked, or cancelled, with no last error code.
- Production contains zero `claim_type=skill` claims in every lifecycle state.
  Live recall therefore correctly omits `APPROVED SKILLS`; no synthetic skill
  was inserted or self-approved. Disposable installed-wheel fixtures prove
  confirmed-only, active-scope injection and candidate/stale/wrong-scope
  exclusion.
- The original observation task never ran. Its invalid 2026-08-08 20:36 trigger
  is replaced by a consoleless P5 runner due 2026-08-09 02:20 Argentina time.

## Remaining live gates

Complete the clean hidden 24-hour check. It must record queue, scope, lineage,
replay, task, provider, latency, gateway, installed-wheel skill isolation, and
the absence of unauthorized production skill injection. It must leave a
failure report and skip push/PR on any failed gate. On success it may push this
branch and create the PR; it may never tag, release, publish, deploy, or merge.

## 2026-08-09 replacement observation — BLOCKED

The post-P5 baseline was used; the invalid pre-P5/OOM window was not used for
longitudinal comparison. The Windows authority was opened read-only and reported
one missing graph job for the governed project scope. That violates the capture
coverage invariant, so this check does not close either the P5 24-hour gate or
the older v4.6 seven-day observation.

Other redacted read-only evidence collected before the stop condition was
healthy: the gateway remained active from the P5 activation timestamp with no
restart or OOM-loop indicator, managed OOM avoidance stayed set, TLS remained
established, both exact P5 wheel hashes were retained on the VM, provider
outbox residue was zero, replica bytes did not change, production had zero
confirmed skills, and the Windows scheduled tasks retained P5 `pythonw.exe`.

No Telegram message was sent; no active database, provider configuration,
credential, firewall, package, release, or deployment state was modified. See
`artifacts/p4-hermes-scope-skills/observation-p5-20260808/24h-failure.md` for
the bounded failure record. Remediate and prove the graph-job invariant, then
start a new clean observation from a fresh baseline; do not reuse this failed
window as a PR gate.

The full `quick_check`/foreign-key scan and the remaining HTTP/authenticated
recall, latency, duplicate, and disposable-canary confirmations were not
accepted after the stop condition. They remain required in the replacement run.

## 2026-08-09 graph-queue remediation — VERIFIED, OBSERVATION PENDING

The missing item was claim `125774` (`mm-bfed`), without recording or exposing
its claim text. Its original confirmation-time graph job had completed. Two
subsequent confidence-only validations changed `claims.updated_at`; graph job
identity used that mutable timestamp, so coverage expected a new graph job even
though claim meaning had not changed. The hourly Dreaming action processed only
existing jobs and did not first call the bounded public `improve` queueing path.

The repair keeps the gate strict:

- graph revision identity is the latest real transition into `confirmed`, with
  `updated_at` retained only as the legacy fallback for rows without an event;
- confirmed-to-confirmed confidence events do not create a new graph revision;
- scheduled Dreaming calls `improve(max_items=25)` before leasing capture work;
- re-confirmation from a non-confirmed state still creates a new revision;
- the worker resolves the same stable identity before extracting.

The authorized live repair queued exactly one `extract_graph` job. The worker
leased and completed it in one attempt with zero errors, retries, blocked jobs,
or partial output. Read-only scope coverage then returned `ok`, zero missing
graph jobs, and no pending/retryable/leased capture job.

Verification evidence:

- focused scheduler/capture/public/graph matrix: 46 passed in 31.08 seconds;
- full non-ML: 4,422 passed, 71 skipped, 97 deselected, one expected xfail,
  15 warnings in 747.08 seconds;
- Ruff on changed Python: passed; full collection: 4,591 tests;
- fresh local backup: `~/.memorymaster/snapshots/memorymaster/`
  `20260809-201437-p5-graph-queue-repair/memorymaster-pre-repair.db`;
- backup and disposable restore were byte-identical (SHA-256
  `178ad1334d52a3ff52286c2aa630c9351c2fb6bfd52b114282335ddddd1e2af8`),
  with matching schema and repair-surface counts and zero relevant orphans;
- the attempted E-drive snapshot produced a Windows CRC read error and was
  renamed with `-INVALID-CRC`; it is not accepted as rollback evidence. The
  previously verified pre-P5 E-drive snapshot remains the broader rollback point.

This does not close the PR gate. The clean wheel built from `d4d9aad` has
SHA-256 `06f78585d2b470e273748851fc1c4df5b912de52186743306af87e3042648d0f`;
it is installed in the side-by-side Windows P5 runtime and `pip check` reports
no broken requirements. A real `MemoryMaster-Dreaming` execution used
`pythonw.exe`, returned `0`, logged the new queue -> capture -> dream sequence,
and left capture coverage `ok` with no active jobs.

The replacement `MemoryMaster-Hermes-24h-Check` preserves its existing
interactive principal, runs the validated headless Codex OAuth runner through
`pythonw.exe`, is hidden and start-when-available, retains a four-hour execution
limit, and has a verified start boundary of 2026-08-10 21:20 ART. Its new
post-repair baseline contains no literal private endpoint, password, or secret.
One uninterrupted clean observation is still required before push or PR. No
tag, release, public publish, deployment, or merge is authorized by this
evidence.

## 2026-08-10 repaired P5 observation — BLOCKED

The observer used only `observation-p5-repair-20260809/baseline.json`; it did
not reuse either invalid earlier window. Read-only evidence that passed: the
repair commit is an ancestor of the observation head; authoritative SQLite
foreign-key validation reported zero violations and `coverage=ok`, with zero
missing graph/claim jobs, expired leases, retryables, blocks, partial jobs,
orphans, duplicate source/evidence identities, implicit global Hermes claims,
and confirmed production skills. The full read-only `quick_check` exceeded the
two-minute observation timeout and is not accepted as passed evidence. The
rollout canary retained one support link and one real transition to confirmed.
Gateway status remained active/running
with result success, zero restarts and OOM/traceback indicators since the
baseline, OOM preference `avoid`, and six established TLS connections.

The native provider remains selected/enabled, the legacy bridge remains
disabled, and the durable VM outbox has 13 completed entries with zero
non-completed or last-error rows. Its fallback replica opened only through
SQLite read-only mode and its bytes did not change during that probe; its size
is not the baseline size, so that counter is recorded rather than treated as
read-only proof. HTTP readiness returned 200 with a healthy DB check and an
authenticated recall returned nonempty governed context without `APPROVED
SKILLS`, matching the zero confirmed production skills. The three Windows P5
tasks retain `pythonw.exe`; Dreaming and Steward last completed with result 0,
while MCP HTTP is the expected running service task.

Focused capture/skills/provider/public/task tests passed 55/55 in 79.74 s;
Ruff on the touched HTTP surface and `git diff --check` passed. The VM retains
the exact Hermes provider wheel hash from the baseline. The E-drive snapshot
exception remains `quarantined-invalid-crc`; the verified local restore and
older verified E-drive rollback point remain the only accepted rollback proof.

This gate is deliberately blocked. The Windows P5 runtime imports
MemoryMaster 4.6.0, but no local repair-wheel artifact matching
`06f78585d2b470e273748851fc1c4df5b912de52186743306af87e3042648d0f` was
available to re-hash, and installed-package metadata does not preserve the
wheel digest. More importantly, all four code modules changed by `d4d9aad`
(`capture/coverage`, `capture/repository`, `knowledge/graph_extraction`, and
`surfaces/scheduled_task`) differ byte-for-byte from the current tree; only
documentation changed after that repair commit. The configured P5 runtime is
therefore not accepted as the repair runtime. Also, `gitleaks` is unavailable, so the required
`origin/main..HEAD` branch scan was not run. The required SQLite `quick_check`
also did not complete in its bounded read-only window. Source-suite success and
prior documentation cannot substitute for any direct gate. No push, PR, tag,
release, publication, deployment, merge, active-database mutation, provider
configuration change, gateway restart, or Telegram message occurred.

## 2026-08-10 observation-gate remediation — READY FOR FRESH OBSERVATION

The failed observation remains immutable incident evidence; this section
corrects its blocker diagnoses with direct, separately replayable evidence.

- Two clean builds from exact repair commit `d4d9aad`, both using source epoch
  `1786319616`, produced byte-identical 969,909-byte wheels with SHA-256
  `828a327b25eafefc944485a06eca71ada5ff7d887446b9c644f28747dfdd9ddd`.
  Both artifacts are retained in the remediation evidence directory.
- The retained wheel payload was compared directly with the installed package:
  353 MemoryMaster files checked, zero missing and zero mismatches in both the
  active runtime and a new staged side-by-side runtime. The prior raw worktree
  check compared different checkout line endings and therefore did not prove a
  runtime mismatch. The historical `06f785...` digest is not reused as the new
  reproducibility claim.
- Gitleaks 8.21.2 scanned the 24 commits in `origin/main..HEAD` with exit `0`
  and zero findings. Its JSON report is retained with the remediation evidence.
- The authoritative 6,792,097,792-byte SQLite file opened read-only with
  `query_only=ON`; `PRAGMA quick_check` completed `ok` in 172.406 seconds. The
  failed observer's two-minute limit was not a valid integrity threshold.
- The earlier disposable skill canary accidentally gave all four fixtures the
  same normalized claim text. Ingest deduplication returned one claim ID, so
  its mixed lifecycle transitions made the assertions contradictory. A fixed
  installed-wheel canary uses distinct titles and markers. Active and staged
  runtimes each pass all nine checks: one confirmed authorized skill included;
  candidate, stale, and wrong-scope skills excluded; ordinary claims clean;
  token budget bounded; and fixture IDs distinct.
- The scheduled wrapper previously returned the Codex child exit `0` even after
  Codex wrote a failure report. A deterministic runner now requires fresh marker
  paths and an explicit success marker. A failure marker returns `21`, missing
  success returns `22`, child failures are preserved, and timeout returns `124`.
  Eight focused tests pass with 98% statement coverage.
- The related capture, scheduled-runtime, ontology-graph, and runner matrix
  passes 25/25 in 6.90 seconds. Ruff passes on the new runner and tests and on
  the repaired package modules; both installed runtimes pass `pip check` and
  CLI/scheduled-task import smoke checks.
- Fresh read-only counters remain healthy: `coverage=ok`, zero missing claim or
  graph jobs, duplicate capture identities, expired leases, retryables, blocked
  jobs, partial jobs, orphans, implicit-global Hermes rows, or confirmed
  production skills. The canary remains confirmed at version 2 with one support
  link and one real confirmed transition.

The existing live runtime was not switched because direct wheel-payload proof
showed it already contains the exact repaired code. A fresh baseline and the
hidden, start-when-available observation task are scheduled for 2026-08-11
23:50 ART with a four-hour limit. The runner's task result can no longer be
green without its success artifact. No rollout lifecycle, provider, credential,
firewall, package, gateway, release, or deployment state was changed; no push,
PR, merge, publication, or operator message occurred. The new 24-hour result
is still required before any PR.

After remediation evidence completed, the mandatory agent-memory checkpoint
added four non-sensitive `project:memorymaster` governance claims documenting
the discovered gate constraints and root causes. It did not change rollout
canary lifecycle, source/evidence capture, graph support, or production skill
state. A fresh read-only baseline taken afterward records 126,864 claims,
129,473 citations, 2,197,734 events, `coverage=ok`, and zero capture anomalies.
This bounded governed write is not presented as live-rollout verification.

## 2026-08-12 repair3 immediate verification — BLOCKED

The August 10 baseline had already elapsed the required 24-hour interval, so
repair3 replayed it instead of fabricating a new clock. The full current
`origin/main..HEAD` range contained 29 commits; Gitleaks 8.21.2 completed with
exit 0 and zero findings. Isolated imports confirmed both installed runtimes,
and the active runtime passed `pip check`.

The authoritative SQLite probe was strictly read-only (`mode=ro`,
`query_only=ON`) and left database bytes unchanged. Foreign-key validation had
zero violations, but capture coverage was `broken`: 17 missing graph jobs, 19
blocked jobs, and 8 partial completed jobs. This violates the strict P5
coverage invariant. The verification stopped at that substantive failure;
remaining live checks were not accepted as a pass. No push, PR, merge, tag,
release, publication, deployment, runtime/provider/gateway/task change, or
PPR-7 work occurred. See
`artifacts/p4-hermes-scope-skills/observation-p5-repair3-20260812/verify-failure.md`
and `verify-result.json` for the bounded record.

## 2026-08-12 repair4 and provider-binding remediation

Repair4 used the already elapsed August 10 baseline instead of restarting the
observation clock. It corrected the verifier's all-scope aggregation. Before
the provider diagnostic, exact `project:memorymaster` coverage was `ok`, with
zero missing claim/graph jobs, blocked or due-retryable work, expired leases,
partial completions, orphans, or foreign-key violations. The historical `user`
backlog was reconciled through public queue/worker APIs without rewriting
immutable diagnostics. The later scheduled Dreaming replay left seven
`user`-scope jobs retryable under the stale OpenAI override described below;
exact `project:memorymaster` coverage remains `ok` with zero anomalies.

Two runtime defects were repaired with adversarial tests. Capture workers now
lease one job only when ready to process it, preventing five-minute batch
leases from expiring behind slow provider calls. The Hermes provider now uses
one bounded stateless JSON-RPC POST against the stateless MCP authority, keeps
short recall and longer durable-delivery timeouts separate, classifies
authority payload rejection as permanent, and sanitizes content before durable
enqueue independently of the host MemoryMaster version.

The blocked outbox row was not a leaked credential. Its legacy metadata stored
redaction finding labels beside producer identity hashes; the authority's
derived payload scan rejected that combination as `hex_token_ctx`. New rows
store a boolean redaction marker instead. An exact-ID purge API accepts only
terminal credential-context legacy envelopes, refuses safe and non-credential
rows, enables SQLite secure deletion, checkpoints WAL, and vacuums. Live row
21 was removed, its serialized bytes are absent from DB/WAL/SHM, and four exact
repair-backup files were deleted and are not recoverable. Sixty completed audit
rows remain intact; the outbox has zero noncompleted rows and `quick_check=ok`.

The VM installed provider matches all 12 files in the retained wheel, live
authenticated recall is nonempty in 0.251 seconds, the gateway is
active/success with zero automatic restarts, `ManagedOOMPreference=avoid`,
zero post-start traceback/OOM indicators, and 11 established TLS connections.
Gitleaks scanned the full branch range after the local unpushed test-fixture
history cleanup and found zero leaks.

The fresh non-ML suite passes with 4,445 tests, 72 skips, 97 deselections, one
expected failure, and 15 dependency deprecation warnings in 1,266.95 seconds.
The HTTP 401 probe diagnosed the active override but not the intended provider
contract. Follow-up inspection found user-level `MEMORYMASTER_DREAM_*` values
selecting OpenAI Terra/Luna even though portable defaults and the operator's
choice were Gemini extraction plus GLM consolidation. Task Scheduler had also
cached those ambient values, and the runner returned exit 0 when capture work
failed if the separate Dreaming phase reported no errors.

Commit `36db18d` makes this correction executable: task registration embeds the
chosen provider/models, clears stale variants, adds isolated `-I` execution,
falls back to the native Task Scheduler API when `schtasks /tr` is too long,
and returns nonzero for capture errors. Ten focused scheduling tests pass; the
scheduled-runner subset has 84 percent coverage. The registered action is now
pinned to `gemini-3.5-flash-lite` and `zai-coding-plan/glm-5.2`. A bounded API
probe returned HTTP 200 in 2.452 seconds, and the single due capture retry then
completed through the normal worker. After the stopped high-demand run's
15-minute lease expired, concurrent Steward work caused three fail-closed
`database is locked` results. Steward completed normally with exit 0; the next
exact task replay then completed with Scheduler exit 0 using Gemini Flash Lite
and GLM 5.2. It extracted, consolidated, and applied eight decisions with zero
errors, recovered the stale run, and left no Dreaming lease. Exact
`project:memorymaster` capture coverage remains `ok` with zero anomalies.

Fresh follow-up gates pass: 60 provider/capture/scheduling tests, 10 focused
scheduling tests, 4,445 non-ML tests, 4,615 collected tests, Ruff, branch-range
Gitleaks across 34 commits, active and staged installed-skill canaries, active
wheel payload parity across 353 files, and `pip check`. A strictly read-only
authoritative SQLite probe returned `quick_check=ok`, zero foreign-key rows,
and unchanged database size/timestamp in 150.57 seconds. The bounded verifier
remains before PR creation. Do not push, create the PR, merge, release, or start
PPR-7 before its fresh success evidence.

## 2026-08-12 repair5 bounded verifier - PASS, PR pending

Repair5 reused the elapsed August 10 baseline and wrote new evidence only in
its own artifact directory. All required repair commits are ancestors of HEAD.
The branch-range Gitleaks replay found zero findings across 36 commits. The
active retained wheel has 353 checked payload files with zero missing or
mismatched files and `pip check` is clean; the retained staged `d4d9aad` wheel
and corrected governed-skill canary also pass.

The fresh scheduled Dreaming task exited 0 with Gemini Flash Lite extraction
and GLM 5.2 consolidation, zero run errors, and no Dreaming lease. The fresh
Steward task exited 0. A read-only SQLite probe completed `quick_check=ok` in
106.562 seconds with zero foreign-key violations and unchanged metadata. Exact
`project:memorymaster` coverage is `ok` with zero anomalies; all-scope expired
leases and due retryables are zero. The rollout canary lineage remains one
confirmed version-2 support-linked transition. Focused P5 coverage passed
154 tests with 15 known warnings; Ruff and `git diff --check` pass. The fresh
4,445-test non-ML evidence remains valid because source changes since it are
documentation and GitNexus metadata only.

Same-day native Hermes provider, gateway, outbox, replica, authenticated
recall, and rollback proof from repair4 was independently retained against
unchanged product source; current Windows runtime/task wiring was rechecked.
This pass authorizes a feature-branch push and PR creation only. It does not
authorize merging, release, publication, deployment, runtime/provider change,
or PPR-7 work.

[PR #189](https://github.com/wolverin0/memorymaster/pull/189) was created from
`feat/hermes-scope-skills` to `main` after the evidence commits were pushed.
It is deliberately unmerged.
