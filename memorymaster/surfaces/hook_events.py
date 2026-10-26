"""Best-effort cross-CLI injection counters; never record memory or prompt text."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path


def append_hook_event(cli, event, count):
    """Append one JSONL counter; telemetry failures must never break a hook."""
    try:
        path = Path(os.environ.get("MEMORYMASTER_HOOK_EVENTS_LOG") or (
            Path.home() / ".memorymaster" / "hook-events.jsonl"
        ))
        record = {"ts": datetime.now(timezone.utc).isoformat(),
                  "cli": cli, "event": event, "count": count}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception:
        pass
