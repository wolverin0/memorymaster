"""Run installed hooks with fake recall and a disposable home, never the live DB."""
import io
import json
import runpy
from pathlib import Path

from memorymaster.surfaces import setup_hooks as setup
from memorymaster.recall import context_hook
from memorymaster.core import hook_log


def test_recall_template_sanitizes_json_prompt_before_retrieval(tmp_path, monkeypatch):
    from memorymaster.recall import remote

    root = Path(__file__).resolve().parents[1]
    template = root / "memorymaster/config_templates/hooks/memorymaster-recall.py"
    source = template.read_text(encoding="utf-8").replace("__MEMORYMASTER_PROJECT_ROOT__", tmp_path.as_posix())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMORYMASTER_RECALL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(hook_log, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(hook_log, "_LOG_FILE", tmp_path / "hook.log")
    seen = []
    monkeypatch.setattr(remote, "hook_recall", lambda query, *a, **kw: seen.append(query) or ("claim\udc8f", "shared"))
    monkeypatch.setattr("sys.stdin", io.StringIO(r'{"prompt":"explain \udc8f recall","session_id":"s"}'))
    output = io.StringIO()
    monkeypatch.setattr("sys.stdout", output)
    exec(compile(source, str(template), "exec"), {"__name__": "__main__"})
    assert seen == ["explain \ufffd recall"]
    assert json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"] == "[MemoryMaster recall]\nclaim\ufffd"
    assert "event=error" not in (tmp_path / "hook.log").read_text(encoding="utf-8")


def test_session_start_template_sanitizes_log_and_context(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    template = root / "memorymaster/config_templates/hooks/memorymaster-session-start.py"
    ns = runpy.run_path(str(template))
    ns["_log"]("error", message="prompt\udc8f")
    log = Path.home() / ".memorymaster/hook_state/hook.log"
    assert "message=prompt\ufffd" in log.read_text(encoding="utf-8")
    main_globals = ns["main"].__globals__
    db = tmp_path / "disposable.db"
    import sqlite3

    sqlite3.connect(db).close()
    main_globals.update(DB_PATH=db, _derive_scope=lambda: "project:test",
                        _query_recent_claims=lambda *a: [{"text": "claim\udc8f"}],
                        _jev_session_active=lambda: False, _query_last_cycle_summary=lambda *a: {},
                        _query_candidate_claims=lambda *a: [], _load_recent_wiki_articles=lambda *a: [],
                        _load_compiled_profile=lambda: "", _jev_session_claims=lambda *a: a[2],
                        _format_context=lambda *a: a[1][0]["text"])
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    output = io.StringIO()
    monkeypatch.setattr("sys.stdout", output)
    import pytest

    with pytest.raises(SystemExit) as exc:
        ns["main"]()
    assert exc.value.code == 0
    assert json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"] == "claim\ufffd"


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
    settings = json.loads((path.parents[1] / 'settings.json').read_text())
    start_hook = next(item for item in settings['hooks']['SessionStart']
                      if any('memorymaster-session-start.py' in h['command'] for h in item['hooks']))
    assert 'compact' in start_hook['matcher'].split('|')
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


def test_installed_hook_skips_trivial_prompts_but_not_short_questions(tmp_path, monkeypatch, capsys):
    """T-1433: "ok" injects nothing; a two-word real question (below the old word-count gate) still recalls."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = install(tmp_path, monkeypatch)
    capsys.readouterr()
    calls = []
    monkeypatch.setattr(context_hook, "recall", lambda q, *a, **k: calls.append(q) or "cited context")
    monkeypatch.setattr(hook_log, "log_hook", lambda *a, **k: None)
    monkeypatch.setenv("MEMORYMASTER_RECALL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MEMORYMASTER_RECALL_SEEN_DIR", str(tmp_path / "seen"))

    def run(prompt):
        data = {"session_id": "s-" + prompt, "cwd": str(tmp_path), "prompt": prompt}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
        runpy.run_path(str(path), run_name="__main__")
        return capsys.readouterr().out

    assert run("ok") == "" and run("dale, gracias") == "" and run("\U0001F44D") == ""
    assert calls == []
    assert "cited context" in run("why 500?")
    assert "cited context" in run("T-1433 status")
    assert calls == ["why 500?", "T-1433 status"]


def _run_recall_hook_logging(tmp_path, monkeypatch, capsys, via, ctx):
    """Run the installed recall hook against a stubbed shared server; returns the log_hook calls."""
    from memorymaster.recall import remote

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = install(tmp_path, monkeypatch)
    capsys.readouterr()
    logged = []
    monkeypatch.setattr(hook_log, "log_hook", lambda hook, event, **f: logged.append((hook, event, f)))
    monkeypatch.setattr(remote, "hook_recall", lambda *a, **k: (ctx, via))
    monkeypatch.setenv("MEMORYMASTER_RECALL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MEMORYMASTER_RECALL_SEEN_DIR", str(tmp_path / "seen"))
    data = {"session_id": "timing", "cwd": str(tmp_path), "prompt": "explain the importer"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
    runpy.run_path(str(path), run_name="__main__")
    capsys.readouterr()
    return logged


def test_installed_hook_logs_its_own_startup_cost_even_when_the_server_was_busy(tmp_path, monkeypatch, capsys):
    """T-1648: the hook's start-up and import time were never logged, and a skipped_busy prompt logged nothing at all."""
    for via, ctx in (("skipped_busy", ""), ("shared", "cited context")):
        logged = _run_recall_hook_logging(tmp_path, monkeypatch, capsys, via, ctx)
        timing = [fields for hook, event, fields in logged if hook == "recall" and event == "timing"]
        assert len(timing) == 1, logged
        fields = timing[0]
        assert fields["via"] == via
        assert fields["import_ms"] >= 0 and fields["recall_ms"] >= 0 and fields["log_ms"] >= 0
        # the deferred import of the remote client and the formatting step are timed, and one clock covers all
        assert fields["remote_import_ms"] >= 0 and fields["format_ms"] >= 0 and fields["deliver_ms"] >= 0
        parts = fields["import_ms"] + fields["remote_import_ms"] + fields["recall_ms"] + fields["format_ms"]             + fields["deliver_ms"] + fields["log_ms"]
        assert fields["total_ms"] >= parts - 1
        assert "boot_ms" in fields  # None where the OS cannot report the process age (the log line drops it)


def test_the_switch_disables_the_process_age_lookup_and_the_hook_timing_line(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MEMORYMASTER_STALL_PROBE", "0")
    from memorymaster.core import stall_probe

    assert stall_probe.process_age_ms() is None
    logged = _run_recall_hook_logging(tmp_path, monkeypatch, capsys, "skipped_busy", "")
    assert not [1 for hook, event, _ in logged if hook == "recall" and event == "timing"]
