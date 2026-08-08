# Hermes scope and governed-skills convergence delta
# Covers: SQLite safety, native Hermes rollout, progressive skills, rollback, and observation gates.
# Key terms: TencentDB Agent Memory, Hermes, session scope, personal-skill-v1, approved skills, snapshot.
# Read when: reviewing this feature branch, operating the activated Windows tasks, or resuming the VM rollout.
# Authority: evidence delta for `.planning/HERMES-SCOPE-SKILLS-INTEGRATION-2026-08-07.md`; ROADMAP.md remains authoritative.
# Status: original activation passed; P5 passes locally, but activation and a fresh observation remain.
# Updated: 2026-08-08 after P5 local verification; no public release was created.

## Verdict

The original branch activation passed its safety gates and the native provider
is active on the Hermes VM. The Tencent-derived P5 progressive approved-skill
path now passes locally, but is not yet installed live. The original 24-hour
window also included a VM OOM/gateway interruption, so it cannot authorize the
PR. Rebuild/activate P5 and complete a fresh clean observation. No tag, GitHub
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

## Remaining live gates

Build and install the P5 packages through the recorded rollback-safe procedure,
then prove one authorized confirmed skill is returned while candidate, stale,
and wrong-scope fixtures are absent. Start a new hidden, consoleless 24-hour
check only after those gates pass. It must record queue, scope, lineage, replay,
task, provider, latency, gateway, and approved-skill isolation evidence; leave
a failure report and skip push/PR on any failed gate; and never tag, release,
publish, deploy, or merge.
