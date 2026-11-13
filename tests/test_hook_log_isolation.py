"""T-2510: tests and probes never write the operator's real ~/.memorymaster/hook_state/hook.log."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from memorymaster.core import hook_log

ROOT = Path(__file__).resolve().parents[1]


class _Sink:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write(self, _text):
        return 0


def test_log_hook_under_the_suite_stays_inside_the_pytest_tree(tmp_path_factory, monkeypatch) -> None:
    """No per-test redirect: the suite itself keeps log_hook off the real home.

    The filesystem calls are recorded, not performed, so this test cannot write the real log even
    when the guarantee is broken (that is how it fails on the unfixed code).
    """
    touched: list[Path] = []
    with monkeypatch.context() as spy:
        spy.setattr(Path, "mkdir", lambda self, *a, **k: touched.append(self))
        spy.setattr(Path, "open", lambda self, *a, **k: touched.append(self) or _Sink())
        hook_log.log_hook("t2510", "probe", marker="isolation")

    base = tmp_path_factory.getbasetemp().resolve()
    assert any(p.name == "hook.log" for p in touched), touched
    for path in touched:
        assert base in path.resolve().parents, f"log_hook reached outside the pytest tree: {path.name} under {path.parent.parent}"


def test_env_state_dir_redirects_log_hook_and_the_latency_guard_reader(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(hook_log.ENV_STATE_DIR, str(tmp_path / "state"))
    hook_log.log_hook("t2510", "env_redirect")
    assert hook_log.log_file() == tmp_path / "state" / "hook.log"
    assert "event=env_redirect" in (tmp_path / "state" / "hook.log").read_text(encoding="utf-8")


def test_explicit_module_patch_wins_over_the_env_var(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(hook_log.ENV_STATE_DIR, str(tmp_path / "from-env"))
    monkeypatch.setattr(hook_log, "_STATE_DIR", tmp_path / "patched")
    monkeypatch.setattr(hook_log, "_LOG_FILE", tmp_path / "patched" / "hook.log")
    hook_log.log_hook("t2510", "patched")
    assert (tmp_path / "patched" / "hook.log").exists()
    assert not (tmp_path / "from-env").exists()


def test_env_state_dir_is_honoured_by_a_subprocess_and_the_home_stays_untouched(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYMASTER_")}
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        PYTHONPATH=str(ROOT),
        MEMORYMASTER_HOOK_STATE_DIR=str(tmp_path / "state"),
    )
    code = "from memorymaster.core.hook_log import log_hook; log_hook('t2510', 'subprocess')"
    subprocess.run([sys.executable, "-c", code], env=env, check=True, timeout=60)

    assert "event=subprocess" in (tmp_path / "state" / "hook.log").read_text(encoding="utf-8")
    assert not (home / ".memorymaster").exists()
