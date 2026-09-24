"""hook.log lines carry a full ISO-8601 date-time, not only HH:MM:SS.

Review F-03 evidence could not date hook activity: ``[HH:MM:SS]`` lines from
different days are indistinguishable once the log spans more than one day.
"""
from __future__ import annotations

import re
from datetime import datetime

from memorymaster.core import hook_log


def test_log_line_starts_with_a_full_iso_datetime_with_offset(tmp_path, monkeypatch):
    monkeypatch.setattr(hook_log, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(hook_log, "_LOG_FILE", tmp_path / "hook.log")

    hook_log.log_hook("recall", "latency", stream="fts5", ms=1.5)

    line = (tmp_path / "hook.log").read_text(encoding="utf-8").strip()
    match = re.match(r"^\[(?P<ts>[^\]]+)\] hook=recall event=latency stream=fts5 ms=1.5$", line)
    assert match, line
    stamp = datetime.fromisoformat(match["ts"])
    assert stamp.tzinfo is not None, "timestamp must carry its UTC offset"
    assert stamp.date() == datetime.now(stamp.tzinfo).date()
