<!-- doc-head: September 20 semantic candidate deployment; September 8 selection evidence retained below -->
Covers: installed wheel identity, migration, live transport verification and rollback.
JEV remains disabled without a credential or held-out efficacy acceptance.
ROADMAP.md remains the only roadmap; earlier evidence remains date-scoped.
<!-- /doc-head -->

## September 20: semantic candidates and optional JEV

The operator authorized completion of installation and live verification.
Source `f8146e64d8b3085491c7f0ededefc7571051cce3` (candidate fixes `cefed52`
plus Windows stdio startup correction) was archived and built into
the wheel without dependency upgrades. Both local-client Python 3.12 and the
isolated scheduled-worker Python match all **414 packaged files**, with installed
imports confirmed. The generic Python used by Hermes delta tasks resolves to
the same verified local-client interpreter. The unpublished version remains
4.8.9; identity is established by hashes, not that version label.

- Installed wheel SHA-256:
  `cddcc637b8f7ce8b665b0e93a2bc94f3aac129f6681ad866c36599a57d45788a`.
- Retained rollback wheel SHA-256:
  `8e6d4ad5eb549c2c41184579ae252d833218ae3c3d13d6a2d244416adac94559`.
- A consistent SQLite backup passed `quick_check` and `foreign_key_check`.
  Migration 25 applied only the additive skill-catalog index, preserved all
  **148,323 claims**, and the query plan uses the index. Post-migration live
  checks returned `ok` and zero foreign-key violations. No restore was performed.
- Initial 34/40-test runs passed, but the root conftest could put checkout
  imports back ahead of the wheel despite isolated interpreter/importlib mode;
  those runs are source evidence only. The final installed gate excludes that
  root conftest and asserts every loaded MemoryMaster module belongs to the
  installed package: **40 passed**, all **154** loaded modules verified under
  site-packages (`verified-installed-tests.xml`, `installed-test-origins.json`).
  The scheduled interpreter also passed the disposable
  public demo, including citation/retirement assertions.
- Initial normal-configuration stdio hybrid recall timed out at 90 seconds.
  Native ML imports stalled after the reader started; NumPy-only preloading
  did not resolve it. Importing SentenceTransformers before startup did. The
  narrow Windows entry-point fix imports only the optional library and keeps
  provider/model construction lazy. Absent/broken imports do not prevent MCP
  startup. Two regression cases failed before the fix; the expanded startup,
  public-MCP, HTTP and architecture gate passed 21 tests. Initial failed probe
  logs remain alongside successful verification; no timeout was increased.
- Final normal configured stdio launcher (no diagnostic/preload wrapper):
  initialize **5.80 s**, hybrid recall **11.66 s**, legacy recall **0.17 s**.
  Nine confirmed claims had nonzero vector contributions using the local
  MiniLM model; seven had citations and two were already uncited in SQLite.
  HTTP initialize was **0.02 s**, recall **7.70 s**, and legacy **0.16 s**.
  These are single live probes, not a throughput benchmark or held-out quality
  comparison. Windows stdio now pays the optional import cost at startup.
- Only the affected scheduled HTTP service was stopped and restarted. The new
  process listens on its configured endpoint. Authenticated MCP initialize,
  tool discovery (51 tools), and governed recall succeeded; unauthenticated
  requests returned 401. The live probe returned seven confirmed claims, five
  with citations and two already uncited in SQLite; citation counts matched
  authoritative storage, so this is not new transport citation loss.
- The live catalog has **zero active confirmed skills**. No claims were promoted
  to manufacture a positive skill result. Filesystem skills are a separate
  catalog and are not implicitly governed MemoryMaster skills.
- `TYPESAFE_API_KEY` is absent from process, user and machine environments.
  JEV remains off. A bounded Spanish/English held-out positive/abstention cohort
  must demonstrate a quality/latency gain over deterministic fallback before
  activation. Mocked integration tests do not establish provider usefulness.

Receipts and retained wheels are under the ignored
`artifacts/semantic-jev-deployment-20260920/` directory in the feature worktree:
`final-manifest.json`, `backup.json`, `migration.json`, `live-integrity.json`,
`final-client-package.json`, `final-scheduled-package.json`, test XML files, and
transport receipts. Backup paths and task configuration remain in local
receipts, not public documentation.

Independent read-only review found no migration or active-sync topology blocker.
The active Hermes sync exchanges unversioned delta databases; a manual full-DB
merge across schema 24/25 must first align versions. Code rollback can retain
the additive index and schema-25 record. Never overwrite new claims by restoring
the pre-deployment backup as a routine code rollback.

Earlier long-lived interactive MCP processes retain their imported code until
their client reconnects; deployment does not terminate unrelated user sessions.

# Selection v2 live deployment

The operator requested completion of the previously deferred operational work.
The tested wheel was installed in both the local-client Python and the isolated
scheduled-worker/HTTP Python. Both matched all 411 packaged files; both passed
the disposable positive/rejected Dreaming, cited recall and retirement journeys.
These checks used installed imports, not an extracted wheel or the checkout.

Final installed wheel SHA-256:
`8e6d4ad5eb549c2c41184579ae252d833218ae3c3d13d6a2d244416adac94559`.
The packaged implementation is exactly the original selection candidate from
`fea3ff6`. Source commit `ba3ec47` additionally carries the import guard and
corrected Windows task installer described below; those scripts are outside the
wheel. Both final installations again matched all 411 files and passed the
disposable positive/rejected lifecycles. The package version remains unpublished 4.8.9.
The pre-deployment wheel was retained with SHA-256
`5d7e97696fb6a159f105216c8743816adaa9ea30796ca85a42dd9bf85bdd982c`.
No dependency upgrade, credential change, production database backup/restore,
historical curation or source-checkout merge was performed.

## Active delivery

- The existing authenticated Hermes MCP/HTTP task was restarted. MCP initialize,
  discovery of 51 tools and a real governed query passed against the restarted
  service. A fresh installed stdio server passed the same protocol/query check.
- The HTTP task also had priority 7 and timed out during concurrent database
  review. It now uses priority 6, with its prior task XML retained for rollback.
  One immediate stop/start attempt raced the old socket and failed with bind
  error 10048; waiting for the old listener to close and starting the known task
  restored service. The final listener process has normal priority.
- Five installed Claude hooks previously prepended the old checkout, which could
  bypass wheel installation. They now append that fallback after installed
  packages. Their original files are retained beside the deployed files.
- Codex and Claude MCP launchers now use `-I -m memorymaster.mcp_server`, preventing
  a repository working directory from shadowing the installed package. Other
  parsed configuration values and credentials were preserved; original config
  files are backed up beside their originals, outside the evidence bundle.
- The actual installed recall hook returned 997 characters of live context and
  imported selection v2 from site-packages.
- Already-running interactive stdio clients are not hot-reloaded by installing
  a wheel or changing launch configuration. Their user-owned sessions were not
  terminated. Fresh sessions use the verified launcher; the autonomous worker,
  HTTP service and per-invocation hooks already execute the installed package.

`scripts/check_installed_selection.py` is the runnable deployment guard: it checks
all package bytes, selection version and optional local client/hook configuration.
It passed against the deployed configuration and failed with exit 1 against a
disposable reconstruction of the old launchers and prepending hooks. This guard
turns the observed import-precedence failure into a repeatable check.

## Real Dreaming execution

The existing Dreaming scheduled job was explicitly launched once after deployment,
without changing its regular trigger, activation flags, provider models or budgets.
Run `dream-e624aa9f3cf045adb583c7baf36cb536` completed at
`2026-09-08T22:41:09.686436+00:00`, with zero errors.

It processed three real captured conversations. Two produced no extracted
candidate; one produced a transient operational-status candidate which the real
consolidator rejected using a v2 receipt and `operational_state` destination.
The run wrote zero candidates and zero proposals. Four real provider calls used
40,589 input and 1,137 output tokens. This verifies an actual rejection through
the deployed capture/provider/selection path; it is not a live precision estimate.
Useful positives remain separately verified by the nine actual-provider
development cases and the installed positive lifecycle checks.

The 345 retained historical extractions remain ineligible for automatic resume.
No historical rows were replayed to manufacture a successful live result.

## Naturally scheduled operational review

The existing review wrapper was replaced from the candidate with its existing
six-hour interval, database, lookback and canary, and expected version 4.8.9.
The scheduler naturally started the first attempt
`review-1bd72d77c71042d4982dc0365c61597b` at
`2026-09-08T22:38:48.590057+00:00`. Its database scan progressed very slowly
through small page reads. A read-only sequential scan of the 7,450,255,360-byte
database took 6.12 seconds; the original SQLite checks then completed, with all
seven phases PASS at `2026-09-08T22:50:07.840475+00:00` (database phase 675.219s).
This first execution received manual cache preparation and is labeled assisted.

The second attempt, `review-54e9b243ded14583915930695a40662a`, tested sequential
preparation inside the task. It remained slow and was explicitly stopped at
`2026-09-08T23:01:25.398349+00:00`, producing INCOMPLETE/exit 9. This experiment
was removed in `ba3ec47`; its temporary wheel and receipts are historical only.

The task used Windows priority 7, which assigns low I/O and memory priority.
The installer now explicitly sets priority 6: normal CPU and disk priority,
with medium memory priority. The mapping is documented by
[Microsoft Task Scheduler](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-priority-settingstype-element).
The six-hour interval, IgnoreNew behavior and 24/25-minute deadlines remain.
The original SQLite integrity and foreign-key checks are unchanged.

The final installer correction passed 35 operational-review, supervisor, E2E
and public-demo tests. An executed installer test intercepts registration in a
disposable environment and checks the effective priority/deadline; changing the
priority back to 7 makes that test fail. Ruff and regenerated release metadata
passed. All 411 final package files remain identical to the 5,118-test candidate.
The third natural scheduler execution,
`review-4238605d78a54c2b8a1cc7a220e5527e`, completed independently at
`2026-09-08T23:08:59.315212+00:00`, with all seven phases PASS and native exit 0.
It took 208.025 seconds overall; the unchanged database check took 206.984s.
No manual cache preparation or process-priority intervention was applied to it.

A final explicitly triggered review also exercised coexistence with the corrected
HTTP task. During `review-d00d1851b6f54962a612eb03f13df994`, both HTTP and fresh
stdio initialize/discovery/real-query checks passed in 2.5 seconds total while
the same database-review phase remained active throughout. This is the final
concurrent-delivery evidence, replacing the earlier HTTP timeout. That review
also completed all seven phases with PASS/exit 0 at
`2026-09-08T23:15:07.638674+00:00`, taking 243.500 seconds overall.

## Evidence and rollback

Local receipts are under `artifacts/live-selection-20260908`: `local-final-verified.json`,
`scheduled-final-verified.json`, `mcp-verified.json`, `hook-live.json`,
`dreaming-live.json`, `hooks-deployed.json`, `import-guard-inverse.json`,
`priority-inverse.json`, `review-natural-attempt.json`, `review-natural-result.json`
and `mcp-concurrent-review.json`.
The rollback subdirectory retains the original, validly named wheel. The previous
review task XML, wrapper and configuration are retained in the same local bundle.
Reinstall that wheel in the two known environments and restore only the changed
launchers/hooks/task configuration if rollback is needed; retain additive ledger
columns and the evidence history. Restart only the identified HTTP owner, waiting
for its old listener to disappear before starting it again.

No human judgments or long-term production quality figures were fabricated.
The historical cohort's human-provenance metric is preserved as historical
evaluation, not assigned to the operator as outstanding technical QA.
