# Lifecycle Edge Cases - 2026-05-11

Scope: `memorymaster/jobs/dedup.py`, `memorymaster/jobs/decay.py`, `memorymaster/jobs/compactor.py`, `memorymaster/jobs/compact_summaries.py`, `memorymaster/auto_resolver.py`, `memorymaster/conflict_resolver.py`, `memorymaster/_storage_lifecycle.py`.

Coverage read: `tests/test_dedup.py`, `tests/test_compact_summaries.py`. GitHub `main` did not expose `tests/test_decay*.py`, `tests/test_compact.py`, or `tests/test_compactor.py` through the available file fetch path; the gaps below treat those as absent unless a local-only file exists.

## Scenario Matrix

### 1. Happy Path

#### HP-1 - Dedup archives a duplicate and preserves lineage
- Trigger: two active claims with identical text and sufficient embedding similarity.
- Flow: `run()` fetches active statuses, `find_duplicates()` emits a `DuplicatePair`, then `transition_claim(... to_status="archived", event_type="dedup", replaced_by_claim_id=...)` runs and `add_claim_link(..., "supersedes")` is attempted.
- Expected outcome: one claim remains active, the archived claim has `replaced_by_claim_id`, a `dedup` transition event exists, and a `supersedes` link exists.
- Suspected current behavior: archive and summary event happen, but link creation failures are swallowed and integration tests only assert that some claim archived.
- Coverage gap: `tests/test_dedup.py:240` checks archive count, not `replaced_by_claim_id`, transition event payload, or `supersedes` link.
- Reference: `memorymaster/jobs/dedup.py:143`, `memorymaster/jobs/dedup.py:205`, `memorymaster/_storage_lifecycle.py:36`.

#### HP-2 - Compact summaries creates a confirmed summary with derived links
- Trigger: at least `min_cluster` archived, unsummarized claims with the same subject and parseable LLM JSON.
- Flow: `_get_unsummarized_archived_claims()` filters archived claims, `run()` clusters them, creates a summary claim, transitions it to confirmed, adds `derived_from` links, and records a compactor event.
- Expected outcome: one confirmed `summary` claim, source archived claims unchanged, all source claims linked from the summary, and event payload captures source IDs.
- Suspected current behavior: works for the simple mocked LLM path; duplicate link failures are counted only by `links_created`, not surfaced as errors.
- Coverage gap: `tests/test_compact_summaries.py:138` asserts summary and link count, but does not assert event payload, source claim status, or partial link failure behavior.
- Reference: `memorymaster/jobs/compact_summaries.py:180`, `memorymaster/jobs/compact_summaries.py:308`, `memorymaster/jobs/compact_summaries.py:373`.

### 2. Error

#### ERR-1 - Decay aborts on malformed timestamps
- Trigger: `store.find_for_decay()` returns a claim whose `updated_at` is not ISO parseable.
- Flow: `run()` calls `_parse_iso()` and no caller-level `try` catches `ValueError`.
- Expected outcome: the bad claim should be skipped with a diagnostic event and later claims should still decay.
- Suspected current behavior: the whole decay run raises before processing remaining claims.
- Coverage gap: no fetched `tests/test_decay*.py`; existing dedup and compact-summary tests do not exercise decay parsing failures.
- Reference: `memorymaster/jobs/decay.py:7`, `memorymaster/jobs/decay.py:25`.

#### ERR-2 - Compactor can archive claims and then fail artifact write
- Trigger: `artifacts_dir` points to an unwritable path or `Path.write_text()` raises after candidates are transitioned.
- Flow: `run()` transitions each claim before `_write_json()` writes `summary_graph.json` and `traceability.json`.
- Expected outcome: compaction should be transactional or emit recovery metadata before changing claim statuses.
- Suspected current behavior: claims may be archived while no artifacts or `compaction_run` event are written.
- Coverage gap: no fetched `tests/test_compact*.py` covers `_write_json()` failure after archive transitions.
- Reference: `memorymaster/jobs/compactor.py:15`, `memorymaster/jobs/compactor.py:47`, `memorymaster/jobs/compactor.py:87`, `memorymaster/jobs/compactor.py:196`.

### 3. Edge Case

#### EDGE-1 - Dedup text overlap ignores punctuation and repeated words poorly
- Trigger: two claims differ only by punctuation or repeated tokens, such as `Postgres 16, pgvector` vs `Postgres 16 pgvector`.
- Flow: `_text_overlap()` lowercases and splits on whitespace into sets, so punctuation remains attached and repetition is discarded.
- Expected outcome: normalized tokenization should treat punctuation-only differences as equivalent.
- Suspected current behavior: false negatives are possible when embeddings are below threshold and text overlap is the second gate.
- Coverage gap: `tests/test_dedup.py:59` covers simple words but not punctuation, repeated words, or Unicode punctuation.
- Reference: `memorymaster/jobs/dedup.py:39`, `memorymaster/jobs/dedup.py:126`.

#### EDGE-2 - Conflict grouping treats whitespace-normalized values as equal but not structured equivalents
- Trigger: same subject/predicate/scope with object values like `{"port":5432}` and `{ "port": 5432 }`.
- Flow: `_normalize_value()` only strips and lowercases before `_build_conflict_groups()` compares distinct values.
- Expected outcome: structured JSON-equivalent values should not be flagged as conflicts.
- Suspected current behavior: syntactic formatting differences become conflicts.
- Coverage gap: conflict tests referenced by GitNexus cover case-insensitive value match, but not JSON, numeric, path, or list normalization.
- Reference: `memorymaster/conflict_resolver.py:124`, `memorymaster/conflict_resolver.py:130`.

### 4. Abuse

#### ABUSE-1 - Dedup can be forced into expensive all-pairs embedding comparisons
- Trigger: many attacker-controlled active claims with similar text and a high `limit` or default 10,000 scan.
- Flow: `run()` fetches up to 10,000 active claims, `find_duplicates()` embeds all filtered claims, then does nested pairwise comparisons.
- Expected outcome: runtime should be bounded by batching, caps, or explicit operator warnings.
- Suspected current behavior: O(n^2) similarity comparisons can monopolize CPU.
- Coverage gap: `tests/test_dedup.py:191` checks a 3-claim chain only, not large-N behavior or runtime guards.
- Reference: `memorymaster/jobs/dedup.py:103`, `memorymaster/jobs/dedup.py:118`, `memorymaster/jobs/dedup.py:184`.

#### ABUSE-2 - Compact summaries can leak sensitive archived text into an LLM prompt
- Trigger: archived claims contain credentials, private paths, or sensitive values and `compact_summaries.run()` is called with a cloud provider.
- Flow: `_build_claim_text_block()` includes subject, predicate, object value, and the first 300 text characters, then `run()` sends it to `_call_llm()`.
- Expected outcome: summarization should reuse the same sensitivity filter/redaction policy as ingest.
- Suspected current behavior: prompt construction performs no sensitivity filtering.
- Coverage gap: `tests/test_compact_summaries.py:138` uses harmless fake data and does not assert redaction before `_call_llm`.
- Reference: `memorymaster/jobs/compact_summaries.py:59`, `memorymaster/jobs/compact_summaries.py:291`, `memorymaster/jobs/compact_summaries.py:296`.

### 5. Scale

#### SCALE-1 - Compactor builds full in-memory graphs before writing
- Trigger: thousands of archive candidates with multiple citations each.
- Flow: `run()` accumulates `claim_nodes`, `citation_nodes`, `edges`, `summary_to_source`, and `claim_lineage_rows` in memory before writing JSON.
- Expected outcome: memory use should be bounded or streamed for large compaction runs.
- Suspected current behavior: memory grows with claims plus citations plus duplicated traceability rows.
- Coverage gap: no fetched compactor tests cover high-volume candidates or artifact size.
- Reference: `memorymaster/jobs/compactor.py:48`, `memorymaster/jobs/compactor.py:65`, `memorymaster/jobs/compactor.py:157`.

#### SCALE-2 - Compact summaries slices large clusters but drops small remainders
- Trigger: cluster size is not divisible by `max_cluster`, with the final slice smaller than `min_cluster`.
- Flow: `run()` chunks each eligible cluster by `max_cluster`, then skips sub-clusters with length below `min_cluster`.
- Expected outcome: every source claim in an eligible large cluster should either be summarized or explicitly reported as skipped.
- Suspected current behavior: remainder claims are silently left unsummarized.
- Coverage gap: `tests/test_compact_summaries.py:260` covers 8 claims split into 4+4, not a 7 claim case with 4+3 or 5+2 boundaries.
- Reference: `memorymaster/jobs/compact_summaries.py:279`, `memorymaster/jobs/compact_summaries.py:284`.

### 6. Concurrent

#### CONC-1 - Dedup pair selection races with concurrent claim updates
- Trigger: another writer updates or archives a candidate after `run()` reads claims but before transition.
- Flow: `run()` builds pairs from stale claim objects, then `transition_claim()` eventually reaches `apply_status_transition()` with optimistic version checking.
- Expected outcome: failed transitions should be visible in result details so operators can retry.
- Suspected current behavior: a warning logs, `claims_archived` remains lower, but returned `pairs` still list the failed archive without an error field.
- Coverage gap: `tests/test_dedup.py:240` does not simulate `ConcurrentModificationError`.
- Reference: `memorymaster/jobs/dedup.py:184`, `memorymaster/jobs/dedup.py:205`, `memorymaster/jobs/dedup.py:222`, `memorymaster/_storage_lifecycle.py:56`.

#### CONC-2 - Two auto resolvers can evaluate the same conflict group
- Trigger: two processes call `auto_resolve_conflicts()` on the same conflicted claims.
- Flow: both read `find_by_status("conflicted")`, group pairs, re-fetch adjacent claims, and call `resolve_conflict_pair()`.
- Expected outcome: one resolver should win and the loser should count the stale pair as skipped without duplicate LLM cost.
- Suspected current behavior: both may call the LLM before one transition fails or re-fetch status excludes the second pair.
- Coverage gap: GitNexus shows auto-resolver unit tests for basic pair outcomes, but no concurrent resolver scenario.
- Reference: `memorymaster/auto_resolver.py:135`, `memorymaster/auto_resolver.py:164`, `memorymaster/auto_resolver.py:181`.

### 7. Temporal

#### TEMP-1 - Future updated_at claims get diagnostic events but no decay
- Trigger: a claim has `updated_at` after the current clock.
- Flow: `run()` clamps negative age to zero, records a `decay` event if raw age is negative, then continues.
- Expected outcome: future timestamp is discoverable and not decayed until clock catches up.
- Suspected current behavior: implemented defensively, but event recording failure is swallowed.
- Coverage gap: no fetched `tests/test_decay*.py` asserts the future timestamp diagnostic event.
- Reference: `memorymaster/jobs/decay.py:25`, `memorymaster/jobs/decay.py:36`.

#### TEMP-2 - Superseded valid_until is set, archived valid_until is not
- Trigger: lifecycle transitions a claim to `superseded` or `archived`.
- Flow: `apply_status_transition()` sets `valid_until` only when `to_status == "superseded"` and `archived_at` only when archived.
- Expected outcome: policy should be explicit: archived claims either remain historically valid or get `valid_until`.
- Suspected current behavior: archived claims retain open-ended validity, which can confuse temporal query semantics.
- Coverage gap: dedup and compactor tests check status counts, not `valid_until` semantics.
- Reference: `memorymaster/_storage_lifecycle.py:43`, `memorymaster/_storage_lifecycle.py:47`.

### 8. Data Variation

#### DATA-1 - Dedup scope filtering may hide cross-scope duplicates
- Trigger: same claim text appears in `project:a` and `project:b`, and `scope_filter` is set.
- Flow: `run()` computes `filtered_claims` for scanned count, then passes all claims plus `scope_filter` to `find_duplicates()`.
- Expected outcome: exact-scope dedup should skip cross-scope duplicates; optional cross-scope mode should report possible duplicates without archiving.
- Suspected current behavior: exact-scope behavior is intentional, but no report of cross-scope duplicates exists.
- Coverage gap: `tests/test_dedup.py:229` uses only default `scope="project"`.
- Reference: `memorymaster/jobs/dedup.py:83`, `memorymaster/jobs/dedup.py:191`.

#### DATA-2 - Compact summaries groups missing subjects as `unknown`
- Trigger: archived claims have no subject but unrelated text.
- Flow: `_cluster_by_subject()` assigns every missing subject to `unknown`, making them eligible for one summary if count reaches `min_cluster`.
- Expected outcome: unrelated unstructured claims should not be summarized together solely because subject is absent.
- Suspected current behavior: missing-subject claims can be clustered together in non-semantic mode.
- Coverage gap: `tests/test_compact_summaries.py:68` checks one `unknown` claim only, not mixed unrelated unknown claims.
- Reference: `memorymaster/jobs/compact_summaries.py:76`, `memorymaster/jobs/compact_summaries.py:269`.

### 9. Permission

#### PERM-1 - Artifact path permissions can leave compaction half-applied
- Trigger: compactor runs with `artifacts_dir` outside writable storage or with read-only permissions.
- Flow: archive transitions happen before `_write_json()` creates parent directories and writes files.
- Expected outcome: permission failures should be detected before status transitions or handled by rollback/retry metadata.
- Suspected current behavior: the same half-applied state as ERR-2.
- Coverage gap: no fetched compactor tests cover permission-denied artifact paths.
- Reference: `memorymaster/jobs/compactor.py:15`, `memorymaster/jobs/compactor.py:87`, `memorymaster/jobs/compactor.py:196`.

#### PERM-2 - Compact summaries rejects cloud provider calls without API keys
- Trigger: non-dry-run compact summary with provider `gemini`, `openai`, or `anthropic` and no key.
- Flow: `run()` builds `effective_keys`, checks all keys are blank, and raises `ValueError`.
- Expected outcome: fail fast before reading or clustering archived claims.
- Suspected current behavior: key validation happens before `_get_unsummarized_archived_claims()`, so no DB read occurs.
- Coverage gap: tests cover fake-key success and LLM errors, not missing-key fail-fast behavior.
- Reference: `memorymaster/jobs/compact_summaries.py:241`, `tests/test_compact_summaries.py:182`.

### 10. Integration

#### INT-1 - Conflict resolver uses mark_superseded rather than transition_claim
- Trigger: deterministic conflict resolver applies a loser transition.
- Flow: `resolve_conflicts()` records a `policy_decision`, calls `mark_superseded()`, which calls `apply_status_transition(... event_type="supersession")`, then `set_supersedes()`.
- Expected outcome: audit trail should show both policy decision and supersession, with consistent replacement pointers.
- Suspected current behavior: two events are recorded, but tests may not assert both or the `supersedes_claim_id` on the winner.
- Coverage gap: GitNexus shows conflict resolver tests for winner choice, not end-to-end event/link audit.
- Reference: `memorymaster/conflict_resolver.py:254`, `memorymaster/_storage_lifecycle.py:100`.

#### INT-2 - Auto resolver and deterministic resolver can disagree on winner criteria
- Trigger: same conflict group is eligible for deterministic resolution and LLM auto resolution.
- Flow: `conflict_resolver._pick_winner()` prioritizes pinned/confidence/freshness/citations/id, while `auto_resolver` delegates to LLM prompt criteria.
- Expected outcome: resolution policy should define precedence and prevent two systems from racing to different winners.
- Suspected current behavior: whichever job runs first wins; no shared arbitration marker exists.
- Coverage gap: separate tests cover each resolver path, but no integration test runs both against one conflict group.
- Reference: `memorymaster/conflict_resolver.py:63`, `memorymaster/auto_resolver.py:82`.

### 11. Recovery

#### REC-1 - Integrity reconciliation reports but does not repair transition/hash issues
- Trigger: event rows contain invalid transitions or broken hash chain links.
- Flow: `reconcile_integrity()` detects transition issues and hash chain issues; in fix mode it skips event deletion and hash rebuild because events are append-only.
- Expected outcome: report should be actionable and distinguish repairable vs non-repairable findings.
- Suspected current behavior: non-repairable issues are reported, but no recovery event is recorded to acknowledge operator review.
- Coverage gap: no fetched lifecycle integrity tests cover append-only recovery decisions.
- Reference: `memorymaster/_storage_lifecycle.py:165`, `memorymaster/_storage_lifecycle.py:211`, `memorymaster/_storage_lifecycle.py:292`.

#### REC-2 - Missing embeddings table is recreated on upsert
- Trigger: `claim_embeddings` table is absent when `upsert_embeddings()` runs.
- Flow: sqlite `OperationalError` with `no such table` causes `_ensure_embeddings_schema()` then a retry insert.
- Expected outcome: embeddings table is recreated and all requested rows upserted once.
- Suspected current behavior: implemented, but retry failure returns `0` instead of raising, which can mask partial recovery failure.
- Coverage gap: no fetched tests exercise missing table recreation or retry failure.
- Reference: `memorymaster/_storage_lifecycle.py:345`, `memorymaster/_storage_lifecycle.py:367`.

### 12. State Transition

#### STATE-1 - Decay can attempt stale transition from statuses that cannot become stale
- Trigger: `find_for_decay()` returns a claim whose current status is not allowed to transition to `stale`.
- Flow: `run()` sets confidence first, then calls `transition_claim(... to_status="stale")` when confidence falls below threshold.
- Expected outcome: either `find_for_decay()` guarantees only valid statuses or decay handles invalid transition after confidence update.
- Suspected current behavior: confidence may be lowered before transition raises, producing partial state.
- Coverage gap: no fetched decay tests verify invalid transition ordering.
- Reference: `memorymaster/jobs/decay.py:48`, `memorymaster/jobs/decay.py:56`.

#### STATE-2 - Dedup can archive conflicted claims without resolving contradiction
- Trigger: duplicate detector sees two `conflicted` claims with high similarity and same subject/predicate.
- Flow: active statuses include `conflicted`, and duplicate archiving does not inspect `object_value` disagreement beyond subject/predicate match.
- Expected outcome: conflicted claims should be excluded from dedup or require value equality before archive.
- Suspected current behavior: a conflicted claim could be archived as a duplicate of the other before conflict resolution runs.
- Coverage gap: `tests/test_dedup.py:166` verifies same object value duplicate behavior, not conflicting object values.
- Reference: `memorymaster/jobs/dedup.py:183`, `memorymaster/jobs/dedup.py:127`.

## Hypothesis Queue

1. High - `compactor.run()` can leave claims archived without artifacts if artifact writes fail after transitions (`memorymaster/jobs/compactor.py:87`, `memorymaster/jobs/compactor.py:196`).
2. High - `dedup.run()` may archive `conflicted` claims that share subject/predicate but disagree on object value (`memorymaster/jobs/dedup.py:183`, `memorymaster/jobs/dedup.py:127`).
3. High - `compact_summaries.run()` may send sensitive archived claim content to cloud LLMs without redaction (`memorymaster/jobs/compact_summaries.py:59`, `memorymaster/jobs/compact_summaries.py:296`).
4. Medium - `decay.run()` can partially update confidence before a stale transition failure (`memorymaster/jobs/decay.py:48`, `memorymaster/jobs/decay.py:56`).
5. Medium - `compact_summaries.run()` silently leaves final sub-clusters smaller than `min_cluster` unsummarized (`memorymaster/jobs/compact_summaries.py:279`, `memorymaster/jobs/compact_summaries.py:284`).
6. Medium - malformed `updated_at` aborts the entire decay run (`memorymaster/jobs/decay.py:7`, `memorymaster/jobs/decay.py:25`).
7. Medium - missing-subject archived claims cluster as `unknown` and may produce misleading summaries (`memorymaster/jobs/compact_summaries.py:76`).
8. Low - `dedup.run()` result pairs do not expose per-pair archive/link failures (`memorymaster/jobs/dedup.py:222`).

## Top 10 Recommended Pytest Skeletons

```python
def test_compactor_artifact_write_failure_does_not_archive_without_traceability(tmp_path):
    ...

def test_dedup_does_not_archive_conflicted_claims_with_different_object_values(tmp_path):
    ...

def test_compact_summaries_redacts_sensitive_claim_text_before_llm(monkeypatch, tmp_path):
    ...

def test_decay_malformed_updated_at_skips_claim_and_continues(tmp_path):
    ...

def test_decay_invalid_stale_transition_does_not_leave_confidence_partially_updated(tmp_path):
    ...

def test_compact_summaries_large_cluster_reports_unsummarized_remainder(tmp_path, monkeypatch):
    ...

def test_compact_summaries_unknown_subjects_do_not_cluster_unrelated_claims(tmp_path):
    ...

def test_dedup_concurrent_archive_failure_is_reported_in_result(tmp_path, monkeypatch):
    ...

def test_conflict_resolution_records_policy_and_supersession_audit_events(tmp_path):
    ...

def test_upsert_embeddings_recreates_missing_table_and_returns_row_count(tmp_path):
    ...
```
