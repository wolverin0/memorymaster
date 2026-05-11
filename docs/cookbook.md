# MemoryMaster Gotcha Cookbook

Compiled from `project:memorymaster` claims with `claim_type` equal to `gotcha` or `bug_root_cause`.
Every bullet cites the source claim ID; no uncited gotchas are included.

## Verbatim Dedup FTS5

- The `verbatim_memories` dedup failure was caused by querying the FTS5 content index with a SHA-256 content hash; the index stores text, not hashes, so the lookup effectively never matched existing rows. (Claim 36009 / mm-5e6d)
- The shipped fix replaced the bogus FTS5 hash lookup with a direct `verbatim_memories` lookup by `session_id` and exact `content`, relying on the existing `idx_verbatim_session` index. (Claim 36009 / mm-5e6d)

## Verbatim DB Bloat Triage

- The 9M-row verbatim growth incident was not normal usage growth; it was concentrated in three OmniClaude orchestrator sessions during Apr 13-20, with 8.7M of 9M rows attributed to `scope='project:omniclaude'` via `source_agent='stop-hook'`. (Claim 36005 / mm-0c43)
- Normal steady-state usage around May 1-3 was about 12k rows per day, so suspicious growth after the dedup fix should be treated as a new bypass or caller problem, not the same root cause by default. (Claim 36005 / mm-0c43; Claim 36009 / mm-5e6d)

## Orchestrator Stop-Hook Firehose

- Monitor-driven orchestrator panes can fire the stop hook on every prompt, including heartbeat-style monitor ticks, causing `verbatim_store.store_transcript` to capture the full conversation context repeatedly. (Claim 36005 / mm-0c43)
- If orchestrators are enabled again, add an ingestion gate such as scope filtering, monitor-tick detection, or sampling for known high-volume orchestrator scopes. (Claim 36005 / mm-0c43)

## Verbatim Cleanup

- The May 3 recovery deleted the overloaded `project:omniclaude` verbatim rows and vacuumed the DB, shrinking it from 7.7GB to 2.2GB after the fix was shipped. (Claim 36009 / mm-5e6d)
- Regression coverage for the dedup fix lives in `tests/test_verbatim_dedup.py`, including an orchestrator-burst simulation proving repeated inserts collapse to unique rows. (Claim 36009 / mm-5e6d)

## Pre-Steward Dedupe

- The v3.13 pre-steward dedupe from PRs #1 and #4 was dead code in production because it was wired into `llm_steward.run_steward()`, while production cron uses `MemoryService.run_cycle()`. (Claim 24171 / mm-ddbb)
- Treat dedupe shadow-mode cron variables from that path as no-ops until dedupe is wired into a production path. (Claim 24171 / mm-ddbb)

## Run-Cycle Boundaries

- `MemoryService.run_cycle()` follows the deterministic extractor, deterministic jobs, validator, decay, and compactor path; it does not call the LLM steward path where the v3.13 dedupe was added. (Claim 24171 / mm-ddbb)
- `claude_cli: exit=1` errors seen in the manual cycle log came from `wiki_engine.absorb()` after `run_cycle`, not from inside `run_cycle`. (Claim 24171 / mm-ddbb)

## Wiki Absorb LLM Work

- If the goal is to skip expensive LLM work on near-duplicates, wiring dedupe into `MemoryService.run_cycle()` will not produce that saving because the LLM work is in wiki absorb, not the deterministic cycle. (Claim 24171 / mm-ddbb)
- Viable salvage paths for the dead dedupe work were to wire it as a pre-validator DB-cleanup stage, wire it into `wiki_engine.absorb()`, or intentionally leave it inactive. (Claim 24171 / mm-ddbb)

## CI Optional ML Dependencies

- CI collection failed because `memorymaster/steward_classifier.py` imported `numpy` unconditionally, but `numpy` was not in base dependencies and was only implied by optional ML dependencies. (Claim 21579 / mm-0a24~2)
- Fix this class of issue by either making optional ML imports lazy or promoting required imports into base dependencies; otherwise importing `memorymaster.service` can fail before tests run. (Claim 21579 / mm-0a24~2)

## Main Branch CI Visibility

- Direct pushes hid five consecutive broken CI states from v3.6.0 through v3.12.0; the first PR made the red status visible. (Claim 21579 / mm-0a24~2)
- Do not blindly trust historical green status for this repo until the missing-dependency failure mode is fixed and validated on the PR path. (Claim 21579 / mm-0a24~2)
