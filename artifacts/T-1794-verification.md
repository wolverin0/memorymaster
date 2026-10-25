<!-- doc-head: T-1794 source verification and retained execution failures -->
<!-- Covers: disposable checks, fixture corrections and navigation limits. -->
<!-- Read when: reviewing the opt-in AO-7 consolidation admission guards. -->
<!-- /doc-head -->
# T-1794 verification

Resumed the existing test-first commit `73ff057` and implementation commit
`84fcf56` without replacing their work. The flag remains default off; no live
campaign, authoritative database or operator home configuration was inspected.
Consolidation activation and real-output remeasurement remain with the lane.

## Fresh checks, 2026-10-25

Each pytest invocation selected exactly one file and used disposable fixtures.

| Check | Result |
|---|---|
| `tests/test_dreaming_backfill_consolidate_guards.py` | 22 passed |
| `tests/test_dreaming_backfill_cli.py` | 40 passed |
| `tests/test_dreaming_backfill_quality.py` | 13 passed |
| `tests/test_public_demo.py` | 2 passed |
| Ruff on both changed production modules and the new test file | Passed |
| `python scripts/generate_release_truth.py`, then `--check` | Generated and verified |
| `python scripts/check_contract_witness.py --base origin/main` | Passed; no orphaned witnesses |
| `python scripts/check_branch_freshness.py` | Zero commits behind origin/main |

The scope tests cover alias keys in subjects and quotes, verified worktree Git
ownership, known canonical repository names, absent signals, generic rules,
ambiguous ownership and reviewed scope propagation through application.
Resolution reuses `_scope_aliases` and `resolve_transcript_workspace_scope`.
Duplicate checks reuse `govern.jobs.extractor.normalize_claim_text`; tests also
cover pagination and scope/tenant boundaries. Sensitivity tests inject a
synthetic credential into each of five candidate fields at the read boundary,
assert the existing `redact_text` filter detects it, verify zero provider calls
and scan every fixture file to prove the rejected text was never persisted.
Unset and zero-valued flags both retain the original consolidation behavior.

## Preserved failures and corrections

The previous run recorded 13 fixture permission errors before behavior ran.
On resume, ordinary pytest again produced 22 setup errors and a cleanup
`PermissionError`. A worktree-local temporary directory alone was insufficient:
Python's Windows owner-only directory mode prevented sandbox access.

A process-local bootstrap preserved inherited directory permissions for test
fixtures (`0o777` instead of `0o700`). This exposed 9 failing tests and 13 passing
tests: four failures omitted the required `citations` argument when seeding
claims, and five used an email address that the existing security filter does
not detect. The fixtures now pass explicit empty citations and use a synthetic
credential with an assertion that the existing filter detects it. Production
security patterns were unchanged. The corrected guard suite passed all 22.

The first public demo attempt failed both tests because `TemporaryDirectory`
also creates owner-only directories through `os.mkdir`. Its successful retry
set `tempfile.tempdir` to a disposable worktree directory and adjusted only
owner-only directory creation beneath that directory. Application code and
machine permissions were unchanged. A pytest-asyncio fixture-scope deprecation
warning was present in the runs.

The successful demo bootstrap was:

```python
import os
import tempfile
from pathlib import Path

root = (Path.cwd() / "artifacts" / "t1794-demo-temp").resolve()
root.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(root)
original = os.mkdir
os.mkdir = lambda path, mode=0o777, *, dir_fd=None: original(
    path,
    0o777 if mode == 0o700 and Path(path).is_relative_to(root) else mode,
    dir_fd=dir_fd,
)
import pytest

raise SystemExit(pytest.main([
    "tests/test_public_demo.py", "-q", "--basetemp=" + str(root / "pytest"),
]))
```

## Navigation limits

Deferred tool discovery exposed no GitNexus or Serena tools.
`python scripts/check_code_navigation.py` reported `INDEX_MISSING`.
Impact/change detection therefore used scoped source searches and Git diffs:
the helper's callers are the consolidation, status, quality population and
application paths in `backfill.py`. This resume changed only tests, generated
release metadata and documentation. No production symbols were edited here.
