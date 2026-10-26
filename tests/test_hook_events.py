from __future__ import annotations

import json
from datetime import datetime

from memorymaster.surfaces.hook_events import append_hook_event


def test_receipts_have_exact_shared_schema(tmp_path, monkeypatch):
    log = tmp_path / "nested" / "events.jsonl"
    monkeypatch.setenv("MEMORYMASTER_HOOK_EVENTS_LOG", str(log))
    append_hook_event("agy", "SessionStart", 2)
    append_hook_event("agy", "Stop", 1)
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(rows) == 2
    assert [row["count"] for row in rows] == [2, 1]
    assert [row["event"] for row in rows] == ["SessionStart", "Stop"]
    for row in rows:
        assert set(row) == {"ts", "cli", "event", "count"}
        assert row["cli"] == "agy"
        assert datetime.fromisoformat(row["ts"]).utcoffset().total_seconds() == 0


def test_unwritable_log_never_blocks_hook(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MEMORYMASTER_HOOK_EVENTS_LOG", str(tmp_path))
    append_hook_event("agy", "Stop", 1)
    assert capsys.readouterr() == ("", "")
