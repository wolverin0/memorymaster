"""Small shared telemetry API, with no private payload or live storage."""
import json


def test_append_hook_event_shape_and_append(tmp_path, monkeypatch):
    from memorymaster.surfaces.hook_events import append_hook_event

    path = tmp_path / "nested/events.jsonl"
    monkeypatch.setenv("MEMORYMASTER_HOOK_EVENTS_LOG", str(path))
    append_hook_event("codex", "SessionStart", 2)
    append_hook_event("codex", "UserPromptSubmit", 1)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2
    assert all(set(row) == {"ts", "cli", "event", "count"} for row in rows)
    assert [(r["cli"], r["event"], r["count"]) for r in rows] == [("codex", "SessionStart", 2), ("codex", "UserPromptSubmit", 1)]


def test_log_failure_is_silent(tmp_path, monkeypatch, capsys):
    from memorymaster.surfaces.hook_events import append_hook_event

    monkeypatch.setenv("MEMORYMASTER_HOOK_EVENTS_LOG", str(tmp_path))
    append_hook_event("codex", "SessionStart", 1)
    assert capsys.readouterr() == ("", "")
