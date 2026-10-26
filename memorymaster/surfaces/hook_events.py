"""Best-effort hook delivery counters; never log prompts or recalled text."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def append_hook_event(cli, event, count):
    """Append one JSONL receipt to the configured hook log, without blocking hooks."""
    try:
        path = Path(os.environ.get("MEMORYMASTER_HOOK_EVENTS_LOG") or (
            Path.home() / ".memorymaster" / "hook-events.jsonl"
        ))
        row = {"ts": datetime.now(timezone.utc).isoformat(), "cli": cli,
               "event": event, "count": int(count)}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")
    except (OSError, ValueError, TypeError):
        pass
