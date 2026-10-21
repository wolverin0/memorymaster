<!-- doc-head: T-1661 shared Git project scope resolution and read-only census -->
<!-- Covers linked worktrees, alias precedence, disposable verification and census limits. -->
<!-- Source-only change; no deployment or authoritative database inspection. -->
<!-- /doc-head -->
# T-1661: stable project scope for captured worktree sessions

`scope_from_cwd` now follows the nearest checkout's `.git` gitfile and its
`commondir` pointer. Relative gitfile paths resolve from the checkout, while
relative common-directory paths resolve from the per-worktree Git directory.
This requires no Git subprocess during capture. Nested directories, `_wt/`,
`.claude/worktrees/`, PR folders, task folders and arbitrary future checkout
names inherit the main project's scope.

Dreaming, MCP project defaults, session bindings, workflow adapters and shipped
SessionStart/Stop hooks use this shared helper. Existing public, transcript,
recall and scheduled-task callers already delegate to it. The `resolve_project`
tool resolves fuzzy aliases to filesystem paths; its query-slug matching stays
separate from deriving scope from a session's working directory.

Explicit aliases for the current folder or checkout take precedence, including
sessions in checkout subdirectories. Otherwise the common project's alias
applies before canonical slug fallback. Existing alias-file reload, malformed
file handling, MCP explicit scope overrides and opt-in hash disambiguation
remain supported. Non-Git collection roots retain Dreaming's `global` sentinel;
session/MCP defaults translate that sentinel to `user`, avoiding implicit global
bindings. A repository containing child repositories keeps its own scope.

Standalone copies/clones with no shared Git metadata still need explicit
aliases. An arbitrary external `--separate-git-dir` location does not reliably
identify the original checkout name; use aliases in that case as well. No
parent project is guessed from a task/PR folder's spelling.

## Read-only census for the lane

Run from this checkout, supplying the database explicitly:

```powershell
python -m scripts.census_single_use_scopes --dry-run --db <sqlite-path>
```

Expected output is one JSON object per suspicious claim, ordered by ID:

```json
{"claim_id": 17, "current_scope": "project:t1661"}
{"claim_id": 24, "current_scope": "project:memorymaster-pr279"}
```

No matches produce no output and exit zero. A missing/unreadable/invalid
database produces a sanitized error and exit one; argument omissions exit two.
The script opens only an existing SQLite database with `mode=ro`, sets
`query_only`, selects only `id` and `scope`, and does not initialize a store,
run migrations, read claim text, or rescope claims. SQLite readers may use WAL
coordination sidecars; this is not an immutable/offline forensic reader.

The census identifies task, PR and explicit worktree naming patterns. It is a
review list, not proof of incorrect ownership: arbitrary names whose Git/source
provenance has been lost cannot be classified from scope alone. Existing claim
repair remains a separate lane/operator action. Only disposable databases were
used for verification; the authoritative database was never opened.

## Verification record

- Branch started at the requested base and was zero commits behind `origin/main`.
- Against the original helper, all seven alias-coverage cases failed with
  disposable checkout scopes, including an arbitrary future naming convention.
- Focused checks passed before broad verification; final counts are recorded
  in the worker completion report. Fixtures include real `git worktree add`,
  absolute/relative pointers, nested checkout aliases, malformed metadata,
  submodules, collection roots and team scope-grant rejection.
- Initial pytest attempts hit Windows temporary-directory `PermissionError`,
  including the requested worktree-local basetemp. A local ignored launcher
  preserved inherited ACLs for disposable pytest directories under `artifacts/`
  instead of Python's sandbox-incompatible `mkdir(mode=0o700)`. Production paths
  and permissions were unchanged. `GIT_CEILING_DIRECTORIES` isolates temporary
  non-Git fixtures from the enclosing worktree during tests.
- The census worker's heavy-slot attempts also stopped before executing tests
  because free memory was below its threshold. Coordinator verification uses
  the disposable fixture launcher; those initial failures were retained.
- The broad non-ML run encountered unrelated benchmark temporary-directory
  access failures. A fail-fast diagnostic preserved the first failure:
  `test_autoresearch_bench_guards.py::test_tokenizer_guard_rejects_changed_tokens`
  failed while creating its disposable SQLite corpus outside the worktree,
  before scope resolution; 298 tests passed, one skipped and 99 ML tests were
  deselected. Broad-suite success is not claimed. Subsequent affected-suite
  verification also redirected stdlib temporary files into `artifacts/`.
- The first affected run retained three failures (721 passed, two skipped):
  a discovery-ceiling escape in parent-folder recall, an evaluator test that
  assumed temporary output was outside the checkout, and a default-policy test
  inheriting the host's enabled precompact-residual flag. The ceiling escape
  was fixed and covered together with checking Git metadata at the ceiling
  itself. The evaluator test now uses a synthetic repository root and keeps
  both acceptance/rejection checks; its production privacy guard is unchanged.
  The isolated test launcher explicitly disables the residual opt-in; tests
  that exercise it still enable it themselves. External-temp attempts failed
  before tests because the sandbox could not create the parent directory.
  All 51 focused edge cases then passed using worktree-local fixtures.
- Independent source review approved the linked-worktree path after fixes for
  nested aliases, implicit global bindings and both discovery-ceiling cases;
  reviewer did not execute tests. Census implementation and independent review
  used two bounded workers, both `gpt-6.1-sol` with `high` effort.
- Final affected-suite run: **723 passed, two skipped**, including every
  `test_*scope*`, `test_*capture*`, `test_dream*` module plus public lifecycle,
  MCP helpers, workflow adapters, shipped hook and local resolver regressions.
  The 39 worktree regressions and 33 census cases all passed. Skips were the
  existing Windows symlink-privilege check and dashboard-port-availability test.
  Disposable census invocation produced exactly the example JSON above.
  Ruff passed on all changed Python files; release truth was regenerated and
  its check passed. The generated source-test count increased from 5571 to 5594.
  Local detailed receipts remain in ignored `artifacts/` (including initial
  failures and the final affected-suite JUnit report); no deployment or live
  claim census was performed.
- GitNexus reported `INDEX_MISSING`. No navigation MCP tools were exposed;
  CLI impact could not select this unindexed worktree, and the installed CLI
  has no `detect_changes` command. Bounded live-source caller/diff checks were
  used without selecting another checkout's index.
