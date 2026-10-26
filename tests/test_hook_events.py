"""Receipts prove hooks fired without storing prompts or governed content."""
import json

from memorymaster.surfaces.hook_events import append_hook_event, main


def test_events_append_exact_contract(tmp_path, monkeypatch, capsys):
    log = tmp_path / "logs/events.jsonl"
    monkeypatch.setenv("MEMORYMASTER_HOOK_EVENTS_LOG", str(log))
    append_hook_event("opencode", "injection", 2)
    assert main(["--cli", "opencode", "--event", "capture", "--count", "3"]) == 0
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert [set(row) for row in rows] == [{"ts", "cli", "event", "count"}] * 2
    assert [(r["cli"], r["event"], r["count"]) for r in rows] == [
        ("opencode", "injection", 2), ("opencode", "capture", 3),
    ]
    assert capsys.readouterr() == ("", "")


def test_telemetry_errors_are_silent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MEMORYMASTER_HOOK_EVENTS_LOG", str(tmp_path))
    append_hook_event("opencode", "injection", 1)
    append_hook_event("opencode", "capture", "bad-count")
    assert capsys.readouterr() == ("", "")
