The validator tried to confirm a replacement before retiring the confirmed tuple rival. The uniqueness failure became `conflicted`, and the validator then superseded the rival anyway. Curation drain subsequently classified that orphan conflict as stale.

Confirmation and retirement now share one SQLite lifecycle transaction, including reciprocal links, source review and rollback. Validator and curation-drain winners use that shared path; an explicit predecessor is excluded from duplicate/conflict arbitration. Conflicts without a current confirmed rival remain pending revalidation. Automatic source/structure promotion policy and pinned/identity boundaries remain enforced. PostgreSQL explicitly rejects the new atomic option.

## Acceptance evidence

- **AC1:** On base `34a0cf4`, the initial regression run produced **3 failed, 3 passed**, reproducing replacement -> conflicted -> stale. Final affected suites: **157 passed**. Coverage includes an already-superseded ancestor, a still-confirmed tuple predecessor, held sources, validator/drain rollback, Dreaming source review, pinned claims, identity boundaries and PostgreSQL rejection.
- **AC2:** Read-only census on 2026-10-20, using SQLite `mode=ro` and `PRAGMA query_only=ON`: **3 claims** — `mm-9e4a~2`, `mm-aeb2~2`, `mm-7357~2`. Selector: current status `stale`, non-null `supersedes_claim_id`, and a `transition` event to `stale` whose details contain `conflicto sin rival confirmado vigente`. All three predecessors are superseded and point back to their replacement. No live repair was run.
- **AC3:** Lifecycle, validator, curation-drain, auto-resolver, conflict-resolver, promotion-policy/structure suites, recovery CLI tests and `tests/test_public_demo.py`: **157 passed**. Ruff on all changed Python files, generated release-truth check and staged whitespace check passed. Independent source review found no remaining material issue after the atomic drain and exception-handling corrections.

Tests used disposable databases only. In this Windows sandbox, a local pytest launcher replaced temporary-directory creation mode `0700` with inherited directory permissions; default pytest temporary directories were inaccessible. No application code or test assertions were changed for that workaround.

Additional checks are not claimed green: the broad non-ML run was interrupted after a temp-root-dependent failure in `test_output_must_remain_outside_repository`; the sandbox denied its external temporary root. An extra steward/parity run produced **211 passed, 2 failed** (`test_proposal_resolution_dashboard_parity`, `test_proposal_resolution_mcp_parity`): fixture proposal creation returned no proposals, alongside blocked provider/model-download proxy requests. These are outside the required affected suites; failure evidence was retained in the task session.

## Lane-only revalidation after GO

Use the intended checkout and its configured `MEMORYMASTER_DB`. The default command is read-only:

```powershell
python -m scripts.revalidate_supersession_drain
```

After lane approval, explicitly select the census claims:

```powershell
python -m scripts.revalidate_supersession_drain --apply --human-id 'mm-9e4a~2' --human-id 'mm-aeb2~2' --human-id 'mm-7357~2'
```

The script rejects unknown, ambiguous, changed or pinned selections before opening a writable store. It uses targeted validator revalidation (`limit=0`), with status changes through `transition_claim` and lifecycle helpers; it does not initialize schemas, run a steward cycle, override promotion policy, or force every selected claim to confirmed. No deployment, service restart or merge was performed.

GitNexus was absent for this worktree, and deferred tool discovery did not expose it. Impact/scope review used current source, scoped caller searches, the staged diff and independent review. Git CLI/GitHub CLI network requests were refused by the sandbox proxy. Authenticated GitHub connector publication was also refused: tree creation requires approval, while this session has approval policy never. No remote branch or PR was created; this body is prepared for lane publication.

🤖 Generated with Codex
