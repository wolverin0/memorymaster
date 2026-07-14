# MemoryMaster Operating Envelope

**Status:** Frozen Phase 0 baseline
**Measured:** 2026-07-11 UTC
**Database access:** Strictly read-only
**Live-data mutations authorized:** None

## Snapshot

| Metric | Value |
|---|---:|
| Claims | 108,217 |
| Events | 1,033,626 |
| Verbatim rows | 1,067,072 |
| Distinct verbatim session IDs | 184,889 |
| Candidate claims | 21,828 |
| Confirmed claims | 18,720 |
| Archived claims | 48,726 |
| Stale claims | 14,656 |
| Conflicted claims | 2,087 |
| Superseded claims | 2,200 |
| SQLite DB | 5.123 GiB |
| WAL | 7.705 MiB |
| Drive used | 85.82% |
| Drive free | 66.06 GiB |

## Candidate age

Snapshot cohort: 21,822 candidates at 2026-07-11T01:49:58Z. Six arrived immediately afterward; the program cohort is frozen at 21,828.

| Age | Count |
|---|---:|
| <1 day | 1,012 |
| 1-3 days | 1,558 |
| 3-7 days | 214 |
| 7-14 days | 127 |
| 14-30 days | 385 |
| 30-60 days | 2,393 |
| 60-90 days | 16,132 |
| >=90 days | 1 |

- P50: 70.737 days
- P90: 74.716 days
- P95: 75.390 days
- P99: 75.679 days
- Maximum: 111.091 days

The target `candidate_age_p95 <= 7 days` currently fails.

## Capacity window

Window: 14 completed UTC days, 2026-06-27 through 2026-07-10.

- Reconstructed candidate inflow: 10,000 / 714.29 per day.
- Candidate dispositions: 12,050 / 860.71 per day.
- Safe 80% intake ceiling: 688 per day.
- Observed intake is 103.7% of the safe ceiling: gate fails.
- Require at least one successful steward cycle per completed UTC day.
- Pass requires a seven-day rolling inflow <=688/day for seven consecutive completed days.

The inflow figure is reconstructed from current candidates plus transition events. It is not authoritative because 2,455 recent `llm-stop-hook` candidates have no creation event.

## Retention and growth gates

Verbatim retention stops at whichever limit is reached first:

1. 30 days of age;
2. 512 MiB raw content;
3. 75,000 most-recent distinct session IDs.

Warn at 80% of any limit. Current 30-day demand is 357.96 MiB and 74,061 distinct session IDs.

Until a 30-day physical-size series exists:

- Warn when seven-day rolling physical DB growth exceeds 64 MiB/day.
- Critical when seven-day rolling growth exceeds 128 MiB/day or any single day exceeds 256 MiB.
- Record DB bytes, WAL bytes, and free-disk bytes daily; replace provisional gates after 30 complete days.

Disk watermarks:

- Warning: >=75% used or <75 GiB free.
- Critical: >=85% used or <50 GiB free.
- Capture hard stop: >=92% used or <10 GiB free.

The current drive is percentage-critical and must be tracked as an external/operator action; this plan does not authorize deletion or compaction of live data.

## Backlog completion

- Frozen cohort: 21,828 candidates at 2026-07-11.
- Target review date: 2026-09-30.
- Completion requires original-cohort `still_reviewable = 0`.
- `confirmed + archived_with_reason + rejected_with_reason = 21,828`.
- Every outcome must have an append-only event.
- Global current candidates must have P95 age <=7 days.
- Global candidate count must be <=4,816, equivalent to seven days at the 688/day safe ceiling.
- Do not pre-allocate confirmed/archive/reject counts; truth review determines the split.

## Instrumentation required

- Emit one authoritative claim-created/entered-candidate event per claim.
- Add steward `run_id`, start/end timestamps, inspected/disposed counts, outcomes, reasons, duration, budget, and failures.
- Add a canonical disposition reason; `rejected` is not a current lifecycle status.
- Record daily DB/WAL/free-disk bytes and backlog snapshots.
- Add canonical session lifecycle and byte counters.

## Measurement queries

- Status counts: `SELECT status, COUNT(*) FROM claims GROUP BY status`.
- Candidate ages: `julianday(snapshot_utc) - julianday(created_at)` for current candidates.
- Reconstructed inflow: claims created in the window whose current status is candidate or that have an event entering/leaving candidate.
- Dispositions: unique claims with validator transition events leaving candidate during the window.
- Verbatim volume: row count, distinct `session_id`, and `SUM(LENGTH(content))` grouped by timestamp windows.
- File sizes and free disk were measured through read-only filesystem metadata.
