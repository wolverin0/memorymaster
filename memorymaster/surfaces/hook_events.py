"""Best-effort hook counters; never log prompt text, paths or memory content."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path


def append_hook_event(cli, event, count):
    """Append one JSONL receipt; telemetry failures must never break a hook."""
    try:
        destination = Path(os.environ.get("MEMORYMASTER_HOOK_EVENTS_LOG") or
                           Path.home() / ".memorymaster" / "hook-events.jsonl")
        record = {"ts": datetime.now(timezone.utc).isoformat(),
                  "cli": str(cli), "event": str(event), "count": max(0, int(count))}
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=True) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--count", type=int, required=True)
    args = parser.parse_args(argv)
    append_hook_event(args.cli, args.event, args.count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
