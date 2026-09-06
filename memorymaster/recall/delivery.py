"""Best-effort, bounded recall delivery state; never stores memory content."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Callable

AUTOMATED_PREFIXES = (
    "[SYSTEM NOTIFICATION - NOT USER INPUT]", "<task-notification>",
    "AUTO-SAVE checkpoint", "FLEET agents=",
)
REPEAT_WINDOW_SECONDS = 300


def is_automated(prompt: str) -> bool:
    return prompt.lstrip().startswith(AUTOMATED_PREFIXES)


def _state_path(session_id: str, state_dir: Path | None = None) -> Path:
    directory = state_dir or Path(os.environ.get(
        "MEMORYMASTER_RECALL_STATE_DIR",
        str(Path.home() / ".memorymaster" / "hook_state" / "recall_delivery"),
    ))
    name = hashlib.sha256(session_id.encode()).hexdigest()
    return directory / (name + ".json")


def reset_session(session_id: str, *, state_dir: Path | None = None) -> None:
    """SessionStart (including compact/resume) permits unchanged context again."""
    if not isinstance(session_id, str) or not session_id:
        return
    try:
        _state_path(session_id, state_dir).unlink(missing_ok=True)
    except OSError:
        pass


def _recent(path: Path, fingerprint: str, now: float) -> bool:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        age = now - float(state["delivered_at"])
        return state["fingerprint"] == fingerprint and 0 <= age < REPEAT_WINDOW_SECONDS
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _remember(path: Path, fingerprint: str, now: float) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"fingerprint": fingerprint, "delivered_at": now}, handle)
        os.replace(temporary, path)
    except OSError:
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def deliver(data: dict, context: str, write: Callable[[str], object], *,
            state_dir: Path | None = None, now: float | None = None) -> bool:
    """Record only after output succeeds; state failures never break recall."""
    if not context.strip():
        return False
    now = time.time() if now is None else now
    session = data.get("session_id")
    path = _state_path(session, state_dir) if isinstance(session, str) and session else None
    fingerprint = hashlib.sha256(json.dumps(
        [data.get("cwd", ""), context], ensure_ascii=False,
    ).encode()).hexdigest()
    if path is not None and _recent(path, fingerprint, now):
        return False
    write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": context,
    }}))
    if path is not None:
        _remember(path, fingerprint, now)
    return True
