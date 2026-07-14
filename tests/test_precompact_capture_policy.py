from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "memorymaster" / "config_templates" / "hooks" / "memorymaster-precompact.py"


def _run_hook(tmp_path: Path, *, maximum: bool) -> subprocess.CompletedProcess[str]:
    hook = tmp_path / "precompact.py"
    hook.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    env = os.environ.copy()
    env.update({"HOME": str(tmp_path / "home"), "USERPROFILE": str(tmp_path / "home")})
    if maximum:
        env["MEMORYMASTER_PRECOMPACT_BLOCKING"] = "1"
    else:
        env.pop("MEMORYMASTER_PRECOMPACT_BLOCKING", None)
    return subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps({"session_id": "precompact-policy"}),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_precompact_is_quiet_by_default(tmp_path: Path) -> None:
    result = _run_hook(tmp_path, maximum=False)
    assert result.returncode == 0
    assert result.stdout == ""
    assert not (tmp_path / "home" / ".memorymaster").exists()


def test_precompact_blocking_is_explicit_maximum_capture(tmp_path: Path) -> None:
    result = _run_hook(tmp_path, maximum=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)["decision"] == "block"
