"""Durable content-free telemetry, including independent hook processes."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from memorymaster.surfaces import hook_events


def test_default_and_override_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORYMASTER_HOOK_EVENTS_LOG", raising=False)
    monkeypatch.setenv("MEMORYMASTER_HOME", str(tmp_path))
    assert hook_events.hook_events_path() == tmp_path / "hook-events.jsonl"
    monkeypatch.setenv("MEMORYMASTER_HOOK_EVENTS_LOG", str(tmp_path / "custom.jsonl"))
    hook_events.append_hook_event("codex", "recall_injected", 1)
    row = json.loads((tmp_path / "custom.jsonl").read_text())
    assert set(row) == {"ts", "cli", "event", "count"}
    assert row["cli"] == "codex" and row["count"] == 1
    assert row["ts"].endswith("+00:00")


@pytest.mark.parametrize("cli,events", [
    ("codex", ("SessionStart", "UserPromptSubmit")),
    ("agy", ("SessionStart", "Stop")),
    ("opencode", ("injection", "capture")),
])
def test_events_append_exact_contract(tmp_path, monkeypatch, cli, events):
    from datetime import datetime

    path = tmp_path / "nested" / "events.jsonl"
    monkeypatch.setenv("MEMORYMASTER_HOOK_EVENTS_LOG", str(path))
    for event, count in zip(events, (2, 1)):
        hook_events.append_hook_event(cli, event, count)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [(row["cli"], row["event"], row["count"]) for row in rows] == [
        (cli, events[0], 2), (cli, events[1], 1),
    ]
    for row in rows:
        assert set(row) == {"ts", "cli", "event", "count"}
        assert datetime.fromisoformat(row["ts"]).utcoffset().total_seconds() == 0


def test_processes_append_without_lost_lines(tmp_path, monkeypatch):
    target = tmp_path / "events.jsonl"
    monkeypatch.setenv("MEMORYMASTER_HOOK_EVENTS_LOG", str(target))
    command = [sys.executable, "-c", "from memorymaster.surfaces.hook_events import append_hook_event; "
               "[append_hook_event('codex','recall_injected',1) for _ in range(20)]"]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    workers = [subprocess.Popen(command, env=env) for _ in range(3)]
    assert [p.wait(timeout=30) for p in workers] == [0, 0, 0]
    rows = [json.loads(line) for line in target.read_text().splitlines()]
    assert len(rows) == 60
    assert sum(row["count"] for row in rows) == 60


def test_size_rotation_retains_bounded_backups(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("MEMORYMASTER_HOOK_EVENTS_LOG", str(path))
    monkeypatch.setattr(hook_events, "MAX_BYTES", 1)
    for count in range(8):
        hook_events.append_hook_event("agy", "recall_injected", count)
    assert json.loads(path.read_text())["count"] == 7
    assert json.loads(Path(f"{path}.1").read_text())["count"] == 6
    assert json.loads(Path(f"{path}.5").read_text())["count"] == 2
    assert not Path(f"{path}.6").exists()


def test_bad_input_and_io_failures_never_break_hooks(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MEMORYMASTER_HOOK_EVENTS_LOG", str(tmp_path / "events"))
    for count in (-1, True, "1", None):
        hook_events.append_hook_event("codex", "recall_injected", count)
    assert not (tmp_path / "events").exists()
    monkeypatch.setenv("MEMORYMASTER_HOOK_EVENTS_LOG", str(tmp_path))
    hook_events.append_hook_event("codex", "recall_injected", 1)
    assert capsys.readouterr() == ("", "")
