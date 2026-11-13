"""Shared structured log for all MemoryMaster hooks (#118).

Every installed hook (``~/.claude/hooks/memorymaster-*.py``) should call
:func:`log_hook` at the start and at each decision point so we can see what
actually fired and what each hook did. Before this existed, 5 of 7 hooks were
silent — the Wave 2-F audit couldn't distinguish "hook skipped on purpose"
from "hook crashed before emitting output."

Design constraints:

* Never crash the caller. A failing hook must never kill the Claude Code
  event pipeline. Every IO path is wrapped in try/except and swallows.
* Zero dependencies. Hooks import this module with a bare
  ``sys.path.insert(0, PROJECT_ROOT)`` bootstrap; we can't rely on stdlib
  extras or third-party packages.
* Human-skim + grep friendly. The format is
  ``[YYYY-MM-DDTHH:MM:SS+HH:MM] hook=<name> session=<id> event=<what> [k=v ...]``
  (local time with its UTC offset; a bare ``HH:MM:SS`` could not tell days
  apart once the log spanned more than one).

Usage::

    from memorymaster.core.hook_log import log_hook
    log_hook("classify", "start", session_id=sid, cwd=cwd)
    ...
    log_hook("classify", "routed", session_id=sid, signals=4)
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from memorymaster.core.text_safety import sanitize_text

ENV_STATE_DIR = "MEMORYMASTER_HOOK_STATE_DIR"

# Explicit overrides (tests patch these). None, the default, resolves on every call from the
# environment and the current home, so a redirected HOME or state dir is honoured even when this
# module was imported first.
_STATE_DIR: Path | None = None
_LOG_FILE: Path | None = None


def state_dir() -> Path:
    if _STATE_DIR is not None:
        return _STATE_DIR
    configured = os.environ.get(ENV_STATE_DIR, "").strip()
    return Path(configured) if configured else Path(os.path.expanduser("~")) / ".memorymaster" / "hook_state"


def log_file() -> Path:
    """The file :func:`log_hook` appends to; readers of the shared log should use this too."""
    return _LOG_FILE if _LOG_FILE is not None else state_dir() / "hook.log"


def _fmt_value(v: Any) -> str:
    s = str(v).replace("\n", " ").replace("\r", " ")
    # Quote only if it contains whitespace or equals signs we'd confuse a grep with.
    if any(ch in s for ch in (" ", "\t", "=")):
        s = s.replace('"', "'")
        return f'"{s}"'
    return s


def log_hook(hook: str, event: str, **fields: Any) -> None:
    """Append a structured line to the shared hook log.

    Silent on any error — hooks must never fail because logging failed.
    """
    try:
        path = log_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().astimezone().isoformat(timespec="seconds")
        parts = [f"[{ts}] hook={hook} event={event}"]
        for key, val in fields.items():
            if val is None:
                continue
            parts.append(f"{key}={_fmt_value(val)}")
        line = sanitize_text(" ".join(parts)) + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        # Hooks must never crash the event pipeline. Swallow.
        pass
