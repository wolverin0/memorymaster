"""The tenant-twin step in hermes-sync.sh never aborts the sync (ROADMAP item 6).

2026-10-03/04 on Hermes: the step was written with a literal backslash-n, which
`bash -n` accepts. At run time bash ran a command named `n`, then `set -u`
aborted on the unset variable, so the twin step never ran and the sync skipped
its cleanup and DONE line twice. This runs the real block under the script's
own shell options, with a failing and a succeeding interpreter.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hermes-sync.sh"
BASH = shutil.which("bash")


def _twin_block() -> str:
    lines = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n").splitlines()
    start = next(i for i, line in enumerate(lines) if "memorymaster.bridges.tenant_twins" in line)
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "fi")
    return "\n".join(lines[start:end + 1])


def test_the_block_has_no_literal_escape_sequences() -> None:
    assert "\\n" not in _twin_block()


@pytest.mark.skipif(BASH is None, reason="needs bash")
@pytest.mark.parametrize(("interpreter", "expected"), [("false", "WARN: tenant twin step failed"), ("echo", "tenant twins: -P -m")])
def test_the_step_reports_and_the_script_continues(interpreter: str, expected: str) -> None:
    script = f"set -euo pipefail\nLOCAL_DB=/tmp/x.db\nMM_PYTHON={interpreter}\n{_twin_block()}\necho AFTER\n"
    run = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert expected in run.stdout and run.stdout.rstrip().endswith("AFTER")
