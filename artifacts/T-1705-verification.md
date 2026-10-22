<!-- doc-head: T-1705 source verification of surrogate-safe recall boundaries -->
<!-- Covers: red-first reproduction, affected checks, scope and runtime limits. -->
<!-- Read when: reviewing this fix or handing it to the lane for Argon review. -->
<!-- /doc-head -->
# T-1705 verification

The reported October 20/22 hook error was reproduced before implementation.
One shared `core.text_safety.sanitize_text` replaces surrogate code points with
U+FFFD and preserves other text. No dependencies or configuration flags were added.

## Red-first evidence

Before source changes, the ten new boundary regressions failed (54 existing
items deselected). They demonstrated strict UTF-8 failures in `deliver()`,
session deduplication, the dense query fingerprint and remote request encoding;
lost hook/metrics log lines; unsanitized prompt transport; and a JSON claim
whose recall block retained the surrogate. A separate focused delivery run
confirmed two failures, including the exception raised through `deliver()`.

The reproductions are:

- `test_deliver_lone_surrogate_prompt_and_session`
- `test_deliver_claim_text_with_lone_surrogate_from_json`
- `test_log_hook_preserves_sanitized_lone_surrogate_prompt`
- `test_lone_surrogate_session_preserves_seen_ids_and_reset`
- `test_query_fingerprint_replaces_lone_surrogates`
- `test_metrics_record_retains_sanitized_surrogates`
- `test_dense_search_sanitizes_the_query_before_transport`
- `test_remote_recall_sanitizes_prompt_and_json_claim_boundaries`
- `test_recall_template_sanitizes_json_prompt_before_retrieval`
- `test_session_start_template_sanitizes_log_and_context`

Initial execution failures are retained here as environment evidence: ten
fixture setup errors using the default temporary directory, ten setup errors
with a missing local parent, and a further attempt blocked by Python's Windows
owner-only temporary-directory ACL. The successful red and green runs used
the ignored local `artifacts/local/T-1705/run_pytest.py` harness. It redirects
home/temp to disposable worktree fixtures and preserves inherited permissions
only when creating those fixture directories. No application permissions were
changed. The inaccessible empty `.tmp/t1705-red` directory could not be removed;
it contains no tracked changes.

## Fresh source verification

| Check | Result |
|---|---|
| New regressions plus parametrized helper contract | 16 passed, 54 deselected |
| Complete affected suites and public demo | 92 passed |
| Additional recall/hook/public suites | 316 passed |
| Ruff on all 15 changed Python files | Passed |
| Release truth generation and `--check` | Passed; 5759 source test functions |

The 92-item run covered `test_text_safety`, `test_recall_delivery`,
`test_hook_log_timestamp`, `test_hook_policy`, `test_dense_recall`,
`test_hook_recall_remote`, `test_recall_hook_installation`,
`test_recall_delivery_contract` and `test_public_demo`.

The 316-item run covered `test_context_hook`, `test_context_hook_extra`,
`test_recall_rendering_integrity`, `test_recall_trivial_and_stale_tasks`,
`test_recall_format`, `test_recall_hook_trim`, `test_prompt_recall_project_scope`,
`test_prompt_recall_governance`, `test_hook_env_isolation`, `test_setup_hooks`,
`test_jev_recall_surfaces`, `test_jev_session_surface` and `test_public_v1`.
Both runs used `-q --tb=short` and separate disposable basetemp directories.
Existing pytest-asyncio/Pydantic deprecation warnings remain.

All six strict encoding sites in delivery, hook policy, dense recall and remote
transport now use the shared helper. Hook logs and metrics sanitize immediately
before writing. Recall and session-start outputs sanitize the context before
JSON serialization; their default ASCII-escaped JSON is UTF-8 safe. Prompt
input is sanitized before retrieval in the recall template. The broad requested
encoding/write inventory was reviewed; unrelated capture, vector-service and
verbatim ingestion paths were not changed.

GitNexus freshness check reported `INDEX_MISSING`; discovery exposed neither
GitNexus nor Serena tools. Native source/reference searches and diff inspection
were used for impact and scope verification. Direct callers are the existing
recall templates, delivery eligibility check, session deduplication, dense metrics
and remote hook caller. Signatures, authorization and state schemas are unchanged.

This is source-tested only. No installed hooks, operator home, authoritative
database, live services, push or merge were used. Full non-ML and unrelated hook
suites were outside this bounded fix. Argon review and PR creation remain with
the lane. The visible text changes only where a surrogate becomes U+FFFD.
