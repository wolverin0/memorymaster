<!-- doc-head: Hermes P5 graph repair verified -->
# Hermes scope and governed-skills convergence delta
# Covers: SQLite safety, native Hermes rollout, progressive skills, rollback, and observation gates.
# Key terms: TencentDB Agent Memory, Hermes, session scope, personal-skill-v1, approved skills, snapshot.
# Read when: reviewing this feature branch, operating the activated Windows tasks, or resuming the VM rollout.
# Authority & Status: ROADMAP.md remains authoritative; P5 stays active and a fresh clean observation still gates push/PR.
# Updated: 2026-08-09 with fail-closed evidence, root cause, repair, live queue completion, and backup exception.
<!-- /doc-head -->

## Verdict

The Tencent-derived P5 progressive approved-skill path now passes locally and
is installed on both the Windows authority and Hermes VM. Rollback artifacts,
consoleless task actions, authenticated live recall, provider outbox, and the
installed-wheel isolation canary pass. The original 24-hour window included a
VM OOM/gateway interruption and did not contain P5, so it cannot authorize the
PR. A replacement check is due 2026-08-09 02:20 Argentina time. No tag, GitHub
Release, package publication, merge, or public deployment is authorized here.

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

This does not close the PR gate. Install the clean fixed wheel, replace and
verify the hidden hourly action, establish a fresh baseline, and require one
uninterrupted clean 24-hour observation before any push or PR. No tag, release,
publish, deploy, or merge is authorized by this evidence.
