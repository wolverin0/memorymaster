"""Run installed hooks with fake recall and a disposable home, never the live DB."""
import io
import json
import runpy
from pathlib import Path

from memorymaster.surfaces import setup_hooks as setup
from memorymaster.recall import context_hook
from memorymaster.core import hook_log


def install(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    home = tmp_path / "claude"
    monkeypatch.setattr(setup, "PROJECT_ROOT", project)
    monkeypatch.setattr(setup, "CLAUDE_DIR", home)
    monkeypatch.setattr(setup, "ask_yn", lambda *a, **k: False)
    setup.install_hooks({"provider": "google", "api_key": "", "model": "fixture"})
    return home / "hooks" / "memorymaster-recall.py"


def test_installed_hook_round_trip(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = install(tmp_path, monkeypatch)
    install(tmp_path, monkeypatch)
    capsys.readouterr()
    calls = []
    monkeypatch.setattr(context_hook, "recall", lambda *a, **k: calls.append(a) or "cited context")
    monkeypatch.setattr(hook_log, "log_hook", lambda *a, **k: None)
    monkeypatch.setenv("MEMORYMASTER_RECALL_STATE_DIR", str(tmp_path / "state"))
    data = {"session_id": "session", "cwd": str(tmp_path), "prompt": "explain the importer"}
    def run(payload):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        runpy.run_path(str(path), run_name="__main__")
        return capsys.readouterr().out
    assert "cited context" in run(data)
    assert run(data) == ""
    assert run({**data, "prompt": "<task-notification> background done"}) == ""
    assert len(calls) == 2
    assert "cited context" in run({**data, "session_id": "other",
                                 "prompt": "Explain <task-notification> from that log"})
    # SessionStart, including compaction/resume, resets delivery before DB access.
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
    try:
        runpy.run_path(str(path.with_name("memorymaster-session-start.py")), run_name="__main__")
    except SystemExit as exc:
        assert exc.code == 0
    capsys.readouterr()
    assert "cited context" in run(data)


def test_custom_recall_hook_is_preserved(tmp_path, monkeypatch):
    path = install(tmp_path, monkeypatch)
    custom = "# locally customized look-ahead briefing\n"
    path.write_text(custom)
    install(tmp_path, monkeypatch)
    assert path.read_text() == custom
    assert Path(str(path) + ".proposed").is_file()


def test_modifications_to_managed_hook_are_preserved(tmp_path, monkeypatch):
    path = install(tmp_path, monkeypatch)
    custom = path.read_text() + "\n# user task look-ahead customization\n"
    path.write_text(custom)
    install(tmp_path, monkeypatch)
    assert path.read_text() == custom
