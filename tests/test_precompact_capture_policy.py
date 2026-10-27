from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "memorymaster" / "config_templates" / "hooks" / "memorymaster-precompact.py"


def _run_hook(
    tmp_path: Path, *, maximum: bool, session_id: str = "precompact-policy"
) -> subprocess.CompletedProcess[str]:
    hook = tmp_path / "precompact.py"
    hook.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    env = os.environ.copy()
    env.update({"HOME": str(tmp_path / "home"), "USERPROFILE": str(tmp_path / "home")})
    env.pop("MEMORYMASTER_HOOK_STATE_DIR", None)
    if maximum:
        env["MEMORYMASTER_PRECOMPACT_BLOCKING"] = "1"
    else:
        env.pop("MEMORYMASTER_PRECOMPACT_BLOCKING", None)
    # "default" means neither flag: the live host exports MEMORYMASTER_PRECOMPACT_RESIDUAL=1
    # and it leaked into this test on 2026-10-25 (bulk gate), making it write the residual.
    env.pop("MEMORYMASTER_PRECOMPACT_RESIDUAL", None)
    return subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps({"session_id": session_id}),
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


def _state_dir(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".memorymaster" / "hook_state"


def test_precompact_blocks_once_then_allows_same_session(tmp_path: Path) -> None:
    first = _run_hook(tmp_path, maximum=True, session_id="warn-once")
    second = _run_hook(tmp_path, maximum=True, session_id="warn-once")
    other = _run_hook(tmp_path, maximum=True, session_id="another-session")

    assert json.loads(first.stdout)["decision"] == "block"
    assert (_state_dir(tmp_path) / "warned_warn-once.marker").exists()
    assert second.returncode == 0
    assert second.stdout == ""  # silent exit 0 = allow compaction
    assert "ALLOWED (already warned" in (_state_dir(tmp_path) / "hook.log").read_text(encoding="utf-8")
    assert json.loads(other.stdout)["decision"] == "block"  # the marker is per session


def test_precompact_allows_when_auto_ingest_saved_recently(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    state.mkdir(parents=True)
    # auto-ingest strips non-[A-Za-z0-9_-] chars from the session id; the hook must match that form
    (state / "ab12_last_save").write_text("15", encoding="utf-8")

    result = _run_hook(tmp_path, maximum=True, session_id="ab/12")

    assert result.returncode == 0
    assert result.stdout == ""
    assert not list(state.glob("warned_*.marker"))
    assert "auto-ingest fired" in (state / "hook.log").read_text(encoding="utf-8")
