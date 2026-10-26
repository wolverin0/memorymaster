"""Content-free, durable hook delivery counters shared by CLI adapters."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5


def hook_events_path() -> Path:
    home = Path(os.environ.get("MEMORYMASTER_HOME", str(Path.home() / ".memorymaster")))
    return Path(os.environ.get("MEMORYMASTER_HOOK_EVENTS_LOG", str(home / "hook-events.jsonl")))


@contextmanager
def _locked(path: Path):
    # A separate lock survives rename/rotation; all hook processes use it.
    with Path(str(path) + ".lock").open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def append_hook_event(cli, event, count):
    """Append ``{ts, cli, event, count}``; logging must never break a hook.

    ``count`` is the number of successful deliveries (normally one), not the
    size of the injected memory. No prompt, claim or session content is stored.
    """
    try:
        if (not isinstance(cli, str) or not cli or len(cli) > 100
                or not isinstance(event, str) or not event or len(event) > 100
                or type(count) is not int or count < 0):
            return
        record = json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                             "cli": cli, "event": event, "count": count}) + "\n"
        path = hook_events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _locked(path):
            if path.exists() and path.stat().st_size + len(record.encode("utf-8")) > MAX_BYTES:
                for number in range(BACKUPS - 1, 0, -1):
                    older = Path(f"{path}.{number}")
                    if older.exists():
                        os.replace(older, Path(f"{path}.{number + 1}"))
                os.replace(path, Path(f"{path}.1"))
            with path.open("a", encoding="utf-8") as handle:
                handle.write(record)
    except (OSError, ValueError, TypeError):
        pass


def main(argv: list[str] | None = None) -> int:
    """Transport OpenCode's receipt arguments to the shared append API."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--count", type=int, required=True)
    args = parser.parse_args(argv)
    append_hook_event(args.cli, args.event, args.count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
