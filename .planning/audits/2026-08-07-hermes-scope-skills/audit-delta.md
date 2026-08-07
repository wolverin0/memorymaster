# Hermes scope and governed-skills convergence delta
# Covers: SQLite safety, Windows activation, native Hermes rollout, rollback, and the running observation.
# Key terms: Hermes MemoryProvider, session scope, personal-skill-v1, pythonw, snapshot, 24-hour observation.
# Read when: reviewing this feature branch, operating the activated Windows tasks, or resuming the VM rollout.
# Authority: evidence delta for `.planning/HERMES-SCOPE-SKILLS-INTEGRATION-2026-08-07.md`; ROADMAP.md remains authoritative.
# Status: activation PASS; the 24-hour observation is running and remains the sole PR gate.
# Updated: 2026-08-07 20:36 America/Buenos_Aires; no public release was created.

## Verdict

The branch is locally converged, the authorized Windows SQLite/runtime is
healthy, and the native provider is active on the actual Hermes VM. Scope,
lineage, replay, provider, OAuth, and Telegram gates pass. This is not yet a
completed PR gate: the 24-hour observation is running until 2026-08-08 20:36
Argentina time. No tag, GitHub Release, package publication, merge, or public
deployment is authorized by this delta.

## Durable storage evidence

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

- Runtime:
  `C:\Users\pauol\.memorymaster\runtime\hermes-scope-skills-20260807`
  with MemoryMaster 4.6.0, Hermes provider 0.1.0, and a clean `pip check`.
- `MemoryMaster-Dreaming` and `MemoryMasterSteward` now execute that runtime's
  `pythonw.exe`; no console-hosted PowerShell action was introduced.
- Dreaming retains `--apply-candidates`. Candidate promotion remains exclusively
  controlled by the steward.
- Public `improve --scope user --max-items 10` queued the one missing graph job.
  A manual scheduled Dreaming run completed with result `0`; capture coverage
  then reported `ok`, zero missing graph jobs, zero pending graph jobs, and one
  completed graph job.
- Final verify-only status: disposable sentinel PASS, Dreaming PASS,
  candidate-apply mode matches, provider readiness true for dream, claim, and
  graph extraction, and both scheduled-task last results are `0`.

## Rollback

Only the two task actions changed. Preserve triggers, principals, and settings;
restore these former action executables if rollback is required:

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

## Remaining live gate

The observation clock started at 2026-08-07 20:36 Argentina time. Hidden task
`MemoryMaster-Hermes-24h-Check` uses `pythonw.exe` and a bounded headless Codex
run at 2026-08-08 20:36. It records queue, scope, lineage, replay, task,
provider, latency, and gateway evidence. It must leave a failure report and
skip push/PR on any failed gate; on a full pass it may push the feature branch
and create the PR to `main`. It must not tag, release, publish, deploy, or merge.
