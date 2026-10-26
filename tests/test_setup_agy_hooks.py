from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from memorymaster.surfaces.setup_agy_hooks import install_agy_hooks


def _install(tmp_path, **kwargs):
    return install_agy_hooks(gemini_config_dir=tmp_path / "target gemini",
                             project_root=Path(__file__).parents[1], db_path=tmp_path / "fixture.db", **kwargs)


def test_installer_idempotent_and_preserves_peer_entries(tmp_path):
    directory = tmp_path / "target gemini" / "config"
    directory.mkdir(parents=True)
    peer = {"peer": {"enabled": False, "Stop": [{"command": "peer"}]},
            "memorymaster": {"Stop": [{"command": "custom"},
                                       {"command": "python audit.py --watched-script memorymaster-agy.py"}],
                             "PostToolUse": [{"matcher": "custom", "hooks": []}]}}
    hooks = directory / "hooks.json"
    hooks.write_text(json.dumps(peer), encoding="utf-8")
    settings = directory.parent / "settings.json"
    settings.write_text("untouched", encoding="utf-8")
    first = _install(tmp_path)
    before = hooks.read_bytes()
    script = Path(first["adapter"]).read_bytes()
    assert _install(tmp_path) == first
    assert hooks.read_bytes() == before
    assert Path(first["adapter"]).read_bytes() == script
    result = json.loads(before)
    assert result["peer"] == peer["peer"]
    assert result["memorymaster"]["PostToolUse"] == peer["memorymaster"]["PostToolUse"]
    assert result["memorymaster"]["Stop"][0] == {"command": "custom"}
    assert result["memorymaster"]["Stop"][1] == peer["memorymaster"]["Stop"][1]
    assert set(result["memorymaster"]) == {"enabled", "SessionStart", "Stop", "PostToolUse"}
    assert settings.read_text() == "untouched"
    for event in ("SessionStart", "Stop"):
        handler = result["memorymaster"][event][-1]
        assert handler["type"] == "command"
        assert handler["timeout"] == 10  # seconds, not milliseconds
        assert handler["command"].split()[-2:] == ["memorymaster-agy.py", event]
        assert '"' not in handler["command"]
    assert not (tmp_path / "fixture.db").exists()


@pytest.mark.parametrize("content", ["{", "[]", '{"memorymaster":null}', '{"memorymaster":{"Stop":{}}}'])
def test_invalid_existing_config_is_preserved(tmp_path, content):
    directory = tmp_path / "target gemini" / "config"
    directory.mkdir(parents=True)
    hooks = directory / "hooks.json"
    hooks.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        _install(tmp_path)
    assert hooks.read_text() == content
    assert not (directory / "memorymaster-agy.py").exists()


def test_registered_command_runs_from_config_with_spaces_and_stays_silent_for_internal_job(tmp_path):
    result = _install(tmp_path)
    config = json.loads(Path(result["hooks"]).read_text())
    command = config["memorymaster"]["SessionStart"][0]["command"].split()
    env = dict(os.environ, MEMORYMASTER_AGY_HOOK_INTERNAL="1", HOME=str(tmp_path), USERPROFILE=str(tmp_path),
               MEMORYMASTER_HOOK_EVENTS_LOG=str(tmp_path / "events.jsonl"))
    process = subprocess.run(command, input='{}', text=True, capture_output=True,
                             cwd=Path(result["hooks"]).parent, env=env, timeout=15)
    assert process.returncode == 0
    assert process.stdout == process.stderr == ""
    assert not (tmp_path / "events.jsonl").exists()


def test_installed_bootstrap_injects_via_real_adapter(tmp_path):
    result = _install(tmp_path)
    bootstrap = Path(result["adapter"])
    # Stub only the recall boundary, retaining installed bootstrap and agy adapter.
    runner = (
        "import runpy,sys; from memorymaster.recall import remote; "
        "remote.hook_recall=lambda *a,**kw: ('fixture cited recall','shared'); "
        f"sys.argv=[{str(bootstrap)!r},'SessionStart']; runpy.run_path({str(bootstrap)!r},run_name='__main__')"
    )
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path),
               MEMORYMASTER_HOOK_EVENTS_LOG=str(tmp_path / "events.jsonl"))
    env.pop("MEMORYMASTER_AGY_HOOK_INTERNAL", None)
    env.pop("AGY_BATCH_PROFILE", None)
    payload = {"conversationId": "fixture", "workspacePaths": [str(tmp_path)], "lastUserInput": "recall project decisions"}
    process = subprocess.run([sys.executable, "-c", runner], input=json.dumps(payload), text=True,
                             capture_output=True, env=env, timeout=15)
    assert process.returncode == 0
    assert process.stderr == ""
    assert json.loads(process.stdout) == {"injectSteps": [{"ephemeralMessage": "[MemoryMaster recall]\nfixture cited recall"}]}
