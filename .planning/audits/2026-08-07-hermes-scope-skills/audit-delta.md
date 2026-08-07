# Hermes scope and governed-skills convergence delta
# Covers: SQLite safety gates, package evidence, Windows activation, rollback, and remaining Hermes VM work.
# Key terms: Hermes MemoryProvider, session scope, personal-skill-v1, pythonw, snapshot, 24-hour observation.
# Read when: reviewing this feature branch, operating the activated Windows tasks, or resuming the VM rollout.
# Authority: evidence delta for `.planning/HERMES-SCOPE-SKILLS-INTEGRATION-2026-08-07.md`; ROADMAP.md remains authoritative.
# Status: Windows convergence and activation PASS; live Hermes shadow, observation, and PR BLOCKED by unreachable VM.
# Updated: 2026-08-07 18:37 America/Buenos_Aires; no public release was created.

## Verdict

The branch is locally converged and the authorized Windows SQLite/runtime
activation is healthy. It is not yet a completed Hermes production rollout.
The actual UbuntuVM/Hermes host could not be located or reached, so provider
installation, shadow recall, Hermes candidate capture, the real 24-hour clock,
and the PR remain open.

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
    `2F2C240588EC0F2A26B446DCB288DD9DDD4AA14A0080DF1496A3912B8295DDCE`

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

## Remaining live gate

The local WSL Ubuntu instance has no Hermes binary, Hermes home, gateway
service, or database replica and is not the documented Hermes VM. Known SSH
aliases/endpoints, available keys, Docker contexts, and expected service ports
did not reveal a reachable target. Hyper-V inventory requires authority the
current account does not have and was not escalated.

Resume only when the real UbuntuVM is running/reachable or its non-secret SSH
alias is known. Then install the provider inactive, record `hermes memory
status`, verify gateway platform state, run read-only shadow recall, enable
Hermes candidate capture, and start the 24-hour observation. Create the PR only
after that observation passes; do not publish a release.
