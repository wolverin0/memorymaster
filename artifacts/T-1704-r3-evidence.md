<!-- doc-head: T-1704 round-three alias review and executed regression evidence -->
<!-- Covers: the single Argon blocker, fault injection, isolated suites and release checks. -->
<!-- Read when: independently reviewing the round-three correction. -->
<!-- /doc-head -->
# T-1704 round-three evidence

Reviewed every entry in `artifacts/inputs/argon-review-r2.json`: one blocker,
no other findings. Implemented its requested whitespace-only `_alias_slug`
helper in discovery and all resolver alias lookups, for both modes. Removed
the mode-dependent normalization branches and the old regex constant.

The review's premise was inaccurate at starting revision `9c18ffb`:
`_SLUG_NORMALIZER_RE` was `re.compile(r"\s+")`, already whitespace-only.
The 24 existing alias cases plus 32 new cases passed before production edits
(56 passed, 49 deselected). The new cases check both modes against competing
literal and punctuation-folded aliases in plain directories, repositories,
checkouts and owners, including multiple spaces and ephemeral `t_12` names.
Existing test assertions were preserved.

Red-first evidence is explicitly a fault injection, not an observed production
baseline defect: before changing production source, the launcher assigned
`re.compile(r"[^a-z0-9-]+")` to the old regex in memory. The 56 alias cases then
produced 30 failures and 26 passes. After introducing the shared helper, the
identical injection produced 56 passes. No source or Git history was reverted.

The first injection launcher attempt imported the installed package before
pytest configured this checkout's import path, causing 56 import failures.
That failed attempt remains in `artifacts/t1704-r3/red.log`; the launcher was
corrected to prepend this worktree before importing the module. The valid
behavioral red is `red-2.log`, reproduced below. No failure log was overwritten.

All pytest processes set `WEZBRIDGE_OPERATOR_ALLOW_HEAVY=1` and executed one
test file at a time. Process HOME/USERPROFILE were redirected before importing
pytest; an autouse launcher fixture redirected them again beneath disposable
test temporary storage. Homes used sibling temporary directories to avoid
creating artifacts in a test's own empty-directory assertions. Existing
conftest fixtures isolated aliases, capture state, snapshots and projections.
No authoritative database was used. The launcher retained the prior Windows
sandbox workaround making temporary-directory mkdir use inherited permissions,
and cleared only the process's inherited precompact residual/capture opt-ins.
All 30 round-one suites were freshly executed: **510 passed, zero failures**.
The Pydantic deprecation warnings in the logs are existing warnings.

Fresh non-pytest checks returned:

```text
python -m ruff check memorymaster/core/scope_utils.py tests/test_scope_utils.py
All checks passed!
python scripts/generate_release_truth.py
Release truth generated.
python scripts/generate_release_truth.py --check
Release truth verified.
python scripts/check_contract_witness.py --base origin/main
contract-witness: ok - 1 archivo(s) de codigo con 1 de tests, sin testigos huerfanos
```

GitNexus/Serena tool discovery found no matching tools; this checkout's
navigation check reported `INDEX_MISSING`. Scoped live references showed
`_git_project_root` is called by the shared resolver and project-root digest;
the shared resolver feeds hook/session scope and transcript capture wrappers.
The new helper only replaces equivalent expressions, preserving signatures
and policies. Native reference searches found no remaining consumers of the
removed private constant. Source/diff inspection substitutes for unavailable
GitNexus impact/change detection; no independent acceptance is claimed.

The following pytest output was captured from the actual runs. Only the
absolute checkout prefix is replaced with `<worktree>`; assertions, progress,
counts, warnings and timings are preserved. The local launcher and per-run
raw logs remain under `artifacts/t1704-r3` and `artifacts/t1704_r3_*.py`.

## Injected fault before production edit

```text
FFF.FFF.FFF.FFF.FFF.FFF..F.F.F...F.F.F...F.F.F...F.F.F..                 [100%]
================================== FAILURES ===================================
<worktree>\tests\test_scope_utils.py:274: AssertionError: assert 'project:memorymaster' == 'project:logical'
<worktree>\tests\test_scope_utils.py:274: AssertionError: assert 'project:memorymaster' == 'project:logical'
<worktree>\tests\test_scope_utils.py:274: AssertionError: assert None == 'project:logical'
<worktree>\tests\test_scope_utils.py:274: AssertionError: assert None == 'project:logical'
<worktree>\tests\test_scope_utils.py:274: AssertionError: assert None == 'project:logical'
<worktree>\tests\test_scope_utils.py:274: AssertionError: assert None == 'project:logical'
<worktree>\tests\test_scope_utils.py:274: AssertionError: assert 'project:memorymaster' == 'project:logical'
<worktree>\tests\test_scope_utils.py:274: AssertionError: assert 'project:memorymaster' == 'project:logical'
<worktree>\tests\test_scope_utils.py:274: AssertionError: assert None == 'project:logical'
<worktree>\tests\test_scope_utils.py:274: AssertionError: assert 'project:my_project' == 'project:logical'
<worktree>\tests\test_scope_utils.py:274: AssertionError: assert 'project:my-project' == 'project:logical'
<worktree>\tests\test_scope_utils.py:274: AssertionError: assert None == 'project:logical'
<worktree>\tests\test_scope_utils.py:274: AssertionError: assert 'project:owner' == 'project:logical'
<worktree>\tests\test_scope_utils.py:274: AssertionError: assert 'project:owner' == 'project:logical'
<worktree>\tests\test_scope_utils.py:274: AssertionError: assert 'project:owner' == 'project:logical'
<worktree>\tests\test_scope_utils.py:274: AssertionError: assert 'project:my_project' == 'project:logical'
<worktree>\tests\test_scope_utils.py:274: AssertionError: assert 'project:my-project' == 'project:logical'
<worktree>\tests\test_scope_utils.py:274: AssertionError: assert None == 'project:logical'
<worktree>\tests\test_scope_utils.py:300: AssertionError: assert 'project:other' == 'project:literal'
<worktree>\tests\test_scope_utils.py:300: AssertionError: assert 'project:other' == 'project:literal'
<worktree>\tests\test_scope_utils.py:300: AssertionError: assert 'project:other' == 'project:literal'
<worktree>\tests\test_scope_utils.py:300: AssertionError: assert 'project:other' == 'project:literal'
<worktree>\tests\test_scope_utils.py:300: AssertionError: assert 'project:other' == 'project:literal'
<worktree>\tests\test_scope_utils.py:300: AssertionError: assert 'project:other' == 'project:literal'
<worktree>\tests\test_scope_utils.py:300: AssertionError: assert 'project:other' == 'project:literal'
<worktree>\tests\test_scope_utils.py:300: AssertionError: assert 'project:other' == 'project:literal'
<worktree>\tests\test_scope_utils.py:300: AssertionError: assert 'project:other' == 'project:literal'
<worktree>\tests\test_scope_utils.py:300: AssertionError: assert 'project:other' == 'project:literal'
<worktree>\tests\test_scope_utils.py:300: AssertionError: assert 'project:other' == 'project:literal'
<worktree>\tests\test_scope_utils.py:300: AssertionError: assert 'project:other' == 'project:literal'
=========================== short test summary info ===========================
FAILED tests/test_scope_utils.py::test_strict_workspace_scope_normalizes_aliases[plain-My_Project]
FAILED tests/test_scope_utils.py::test_strict_workspace_scope_normalizes_aliases[plain-My.Project]
FAILED tests/test_scope_utils.py::test_strict_workspace_scope_normalizes_aliases[plain-t_12]
FAILED tests/test_scope_utils.py::test_strict_workspace_scope_normalizes_aliases[deleted-My_Project]
FAILED tests/test_scope_utils.py::test_strict_workspace_scope_normalizes_aliases[deleted-My.Project]
FAILED tests/test_scope_utils.py::test_strict_workspace_scope_normalizes_aliases[deleted-t_12]
FAILED tests/test_scope_utils.py::test_strict_workspace_scope_normalizes_aliases[ancestor-My_Project]
FAILED tests/test_scope_utils.py::test_strict_workspace_scope_normalizes_aliases[ancestor-My.Project]
FAILED tests/test_scope_utils.py::test_strict_workspace_scope_normalizes_aliases[ancestor-t_12]
FAILED tests/test_scope_utils.py::test_strict_workspace_scope_normalizes_aliases[repository-My_Project]
FAILED tests/test_scope_utils.py::test_strict_workspace_scope_normalizes_aliases[repository-My.Project]
FAILED tests/test_scope_utils.py::test_strict_workspace_scope_normalizes_aliases[repository-t_12]
FAILED tests/test_scope_utils.py::test_strict_workspace_scope_normalizes_aliases[checkout-My_Project]
FAILED tests/test_scope_utils.py::test_strict_workspace_scope_normalizes_aliases[checkout-My.Project]
FAILED tests/test_scope_utils.py::test_strict_workspace_scope_normalizes_aliases[checkout-t_12]
FAILED tests/test_scope_utils.py::test_strict_workspace_scope_normalizes_aliases[owner-My_Project]
FAILED tests/test_scope_utils.py::test_strict_workspace_scope_normalizes_aliases[owner-My.Project]
FAILED tests/test_scope_utils.py::test_strict_workspace_scope_normalizes_aliases[owner-t_12]
FAILED tests/test_scope_utils.py::test_workspace_aliases_preserve_literal_keys[plain-My_Project-strict]
FAILED tests/test_scope_utils.py::test_workspace_aliases_preserve_literal_keys[plain-My.Project-strict]
FAILED tests/test_scope_utils.py::test_workspace_aliases_preserve_literal_keys[plain-t_12-strict]
FAILED tests/test_scope_utils.py::test_workspace_aliases_preserve_literal_keys[repository-My_Project-strict]
FAILED tests/test_scope_utils.py::test_workspace_aliases_preserve_literal_keys[repository-My.Project-strict]
FAILED tests/test_scope_utils.py::test_workspace_aliases_preserve_literal_keys[repository-t_12-strict]
FAILED tests/test_scope_utils.py::test_workspace_aliases_preserve_literal_keys[checkout-My_Project-strict]
FAILED tests/test_scope_utils.py::test_workspace_aliases_preserve_literal_keys[checkout-My.Project-strict]
FAILED tests/test_scope_utils.py::test_workspace_aliases_preserve_literal_keys[checkout-t_12-strict]
FAILED tests/test_scope_utils.py::test_workspace_aliases_preserve_literal_keys[owner-My_Project-strict]
FAILED tests/test_scope_utils.py::test_workspace_aliases_preserve_literal_keys[owner-My.Project-strict]
FAILED tests/test_scope_utils.py::test_workspace_aliases_preserve_literal_keys[owner-t_12-strict]
30 failed, 26 passed, 49 deselected in 0.99s
```

## Same injection after shared helper

```text
........................................................                 [100%]
56 passed, 49 deselected in 0.72s
```

## tests/test_auto_ingest_hook_citations.py

```text
...                                                                      [100%]
3 passed in 1.07s
```

## tests/test_auto_ingest_hook_schema.py

```text
........                                                                 [100%]
8 passed in 0.32s
```

## tests/test_context_hook.py

```text
.......................                                                  [100%]
23 passed in 0.63s
```

## tests/test_context_hook_extra.py

```text
......................................................................   [100%]
70 passed in 5.69s
```

## tests/test_contract_witness_check.py

```text
...........                                                              [100%]
11 passed in 0.32s
```

## tests/test_dreaming_backfill_cli.py

```text
........................                                                 [100%]
24 passed in 9.99s
```

## tests/test_dreaming_backfill_operational_review.py

```text
......                                                                   [100%]
6 passed in 0.31s
```

## tests/test_dreaming_backfill_quality.py

```text
.............                                                            [100%]
13 passed in 3.23s
```

## tests/test_dreaming_backfill_rollback.py

```text
........................                                                 [100%]
24 passed in 10.24s
```

## tests/test_dreaming_backfill_windowing.py

```text
....                                                                     [100%]
4 passed in 0.50s
```

## tests/test_dreaming_capture.py

```text
......                                                                   [100%]
6 passed in 0.44s
```

## tests/test_hook_env_isolation.py

```text
.......                                                                  [100%]
7 passed in 0.31s
```

## tests/test_hook_log_timestamp.py

```text
.                                                                        [100%]
1 passed in 0.29s
```

## tests/test_hook_recall_remote.py

```text
..............                                                           [100%]
============================== warnings summary ===============================
tests/test_hook_recall_remote.py::test_shared_server_route_serves_the_warm_recall_behind_the_token
  <worktree>\memorymaster\surfaces\mcp_server.py:110: PydanticDeprecatedSince20: Support for class-based `config` is deprecated, use ConfigDict instead. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.13/migration/
    class _ToolInput(BaseModel):

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
14 passed, 1 warning in 1.05s
```

## tests/test_precompact_capture_policy.py

```text
..                                                                       [100%]
2 passed in 0.45s
```

## tests/test_prompt_recall_project_scope.py

```text
.....                                                                    [100%]
5 passed in 1.68s
```

## tests/test_public_demo.py

```text
..                                                                       [100%]
2 passed in 3.72s
```

## tests/test_public_v1.py

```text
............                                                             [100%]
12 passed in 4.00s
```

## tests/test_recall_hook_installation.py

```text
......                                                                   [100%]
6 passed in 0.85s
```

## tests/test_recall_hook_trim.py

```text
..........................                                               [100%]
26 passed in 4.65s
```

## tests/test_release_truth.py

```text
............                                                             [100%]
12 passed in 2.40s
```

## tests/test_scope_aliases.py

```text
.....                                                                    [100%]
5 passed in 0.29s
```

## tests/test_scope_utils.py

```text
........................................................................ [ 68%]
.................................                                        [100%]
105 passed in 1.18s
```

## tests/test_scope_utils_canonicalize.py

```text
........................                                                 [100%]
============================== warnings summary ===============================
memorymaster\surfaces\mcp_server.py:110
  <worktree>\memorymaster\surfaces\mcp_server.py:110: PydanticDeprecatedSince20: Support for class-based `config` is deprecated, use ConfigDict instead. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.13/migration/
    class _ToolInput(BaseModel):

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
24 passed, 1 warning in 0.84s
```

## tests/test_scope_worktrees.py

```text
.........................................                                [100%]
============================== warnings summary ===============================
memorymaster\surfaces\mcp_server.py:110
  <worktree>\memorymaster\surfaces\mcp_server.py:110: PydanticDeprecatedSince20: Support for class-based `config` is deprecated, use ConfigDict instead. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.13/migration/
    class _ToolInput(BaseModel):

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
41 passed, 1 warning in 1.29s
```

## tests/test_session_end_capture_hook.py

```text
.                                                                        [100%]
1 passed in 0.46s
```

## tests/test_session_scope_binding.py

```text
..........                                                               [100%]
10 passed in 2.52s
```

## tests/test_session_scope_surfaces.py

```text
.....                                                                    [100%]
============================== warnings summary ===============================
memorymaster\surfaces\mcp_server.py:110
  <worktree>\memorymaster\surfaces\mcp_server.py:110: PydanticDeprecatedSince20: Support for class-based `config` is deprecated, use ConfigDict instead. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.13/migration/
    class _ToolInput(BaseModel):

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
5 passed, 1 warning in 1.59s
```

## tests/test_setup_hooks.py

```text
.....................................                                    [100%]
============================== warnings summary ===============================
tests/test_setup_hooks.py::TestMainNonInteractive::test_exit_zero_and_wires_hooks_mcp
  <worktree>\memorymaster\surfaces\mcp_server.py:110: PydanticDeprecatedSince20: Support for class-based `config` is deprecated, use ConfigDict instead. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.13/migration/
    class _ToolInput(BaseModel):

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
37 passed, 1 warning in 11.29s
```

## tests/test_stop_hook_capture_policy.py

```text
...                                                                      [100%]
3 passed in 0.98s
```
