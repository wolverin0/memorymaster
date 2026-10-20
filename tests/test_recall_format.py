"""Injection format modes of the prompt-recall hook (T-1632, AO-0): current | header | gated."""
import io
import json
import runpy

import pytest

from memorymaster.core import hook_log
from memorymaster.recall import context_hook, recall_format
from memorymaster.surfaces import setup_hooks as setup

CTX = "\n".join([
    "# Memory Context",
    "When a memory below informs your reply, cite its [mm-id].",
    "",
    "- [mm-a1] Deploy the billing worker with the staging flag first",
    "- [mm-b2] Use pytest fixtures for temporary databases",
    "- [mm-c3] Invoice rounding follows the AFIP rules for tax totals",
])
MANY = CTX + "\n" + "\n".join(f"- [mm-x{i}] filler claim number {i}" for i in range(6))


def ids(text):
    return [line.split("]")[0].split("[")[1] for line in text.splitlines() if line.startswith("- [mm-")]


def test_current_mode_changes_nothing(monkeypatch):
    monkeypatch.delenv(recall_format.FORMAT_ENV, raising=False)
    assert recall_format.format_mode() == "current"
    assert recall_format.apply_format(CTX, "anything at all") == CTX
    monkeypatch.setenv(recall_format.FORMAT_ENV, "no-such-mode")  # a typo must never change behaviour
    assert recall_format.format_mode() == "current"
    assert recall_format.apply_format(CTX, "anything at all") == CTX


def test_header_mode_has_three_line_header_and_ranks_task_first(monkeypatch):
    monkeypatch.setenv(recall_format.FORMAT_ENV, "header")
    out = recall_format.apply_format(CTX, "unrelated words here", task="fix invoice rounding totals")
    lines = out.splitlines()
    assert lines[0] == "# Decisions you made earlier (cite [mm-id] when you rely on one)"
    assert lines[2] == "" and lines[3].startswith("- [mm-")  # exactly three header lines
    assert ids(out)[0] == "mm-c3"  # the claim matching the active task comes first
    assert sorted(ids(out)) == ["mm-a1", "mm-b2", "mm-c3"]  # nothing lost, bullet text intact
    assert "- [mm-c3] Invoice rounding follows the AFIP rules for tax totals" in lines


def test_header_mode_caps_at_five_claims(monkeypatch):
    monkeypatch.setenv(recall_format.FORMAT_ENV, "header")
    assert len(ids(recall_format.apply_format(MANY, "billing worker deploy"))) == 5


def test_gated_mode_injects_only_when_the_top_claim_is_relevant(monkeypatch):
    monkeypatch.setenv(recall_format.FORMAT_ENV, "gated")
    hit = recall_format.apply_format(CTX, "how do I deploy the billing worker?")
    assert ids(hit)[0] == "mm-a1" and hit.startswith("# Memory Context")  # current wrapper, ranked
    assert recall_format.apply_format(CTX, "what is the weather today") == ""
    assert recall_format.apply_format(CTX, "ok") == ""  # no usable query tokens: nothing to justify it


def test_unparseable_context_passes_through_every_mode(monkeypatch):
    for mode in ("current", "header", "gated"):
        monkeypatch.setenv(recall_format.FORMAT_ENV, mode)
        assert recall_format.apply_format("no bullets here", "prompt text") == "no bullets here"


def test_active_task_text_reads_a_fresh_task(tmp_path):
    assert recall_format.active_task_text(str(tmp_path)) == ""  # no file: no task, never an error


def install(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    home = tmp_path / "claude"
    monkeypatch.setattr(setup, "PROJECT_ROOT", project)
    monkeypatch.setattr(setup, "CLAUDE_DIR", home)
    monkeypatch.setattr(setup, "ask_yn", lambda *a, **k: False)
    setup.install_hooks({"provider": "google", "api_key": "", "model": "fixture"})
    return home / "hooks" / "memorymaster-recall.py"


@pytest.mark.parametrize("mode,prompt,expected", [
    (None, "what is the weather today", "# Memory Context"),
    ("current", "what is the weather today", "# Memory Context"),
    ("header", "what is the weather today", "# Decisions you made earlier"),
    ("gated", "how do I deploy the billing worker?", "# Memory Context"),
    ("gated", "what is the weather today", None),
])
def test_installed_template_applies_the_mode(tmp_path, monkeypatch, capsys, mode, prompt, expected):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = install(tmp_path, monkeypatch)
    capsys.readouterr()
    monkeypatch.setattr(context_hook, "recall", lambda *a, **k: CTX)
    monkeypatch.setattr(hook_log, "log_hook", lambda *a, **k: None)
    monkeypatch.setenv("MEMORYMASTER_RECALL_STATE_DIR", str(tmp_path / "state"))
    if mode is None:
        monkeypatch.delenv(recall_format.FORMAT_ENV, raising=False)
    else:
        monkeypatch.setenv(recall_format.FORMAT_ENV, mode)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "s", "cwd": str(tmp_path), "prompt": prompt})))
    runpy.run_path(str(path), run_name="__main__")
    out = capsys.readouterr().out
    if expected is None:
        assert out == ""
        return
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("[MemoryMaster recall]\n" + expected)
    assert "- [mm-a1] Deploy the billing worker" in context
