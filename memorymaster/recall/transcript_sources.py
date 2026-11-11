"""Where each CLI keeps its sessions and how a record becomes ``(role, text)`` for the transcript index (T-2277).

One parser per format: Codex rollouts, agy exports and OpenCode stores are read by the dreaming adapters
(``dreaming/capture.py``, ``dreaming/agy_source.py``); this module only discovers sessions and hands their
messages to ``recall/transcript_index.py``, which sanitises, scopes and chunks them like Claude's.  Every source
has a kill switch, ``MEMORYMASTER_TRANSCRIPT_SOURCE_<CLI>=0``, and a root override, ``MEMORYMASTER_TRANSCRIPT_ROOT_<CLI>``.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from memorymaster.dreaming import capture
from memorymaster.dreaming.agy_source import discover_agy_sessions, extract_agy_session, read_executor_metadata

CLIS = ("claude", "codex", "agy", "opencode")
LINE_CLIS = ("claude", "codex")  # append-only JSONL, parsed incrementally; agy/opencode are re-read whole
_REL = {"claude": ".claude/projects", "codex": ".codex/sessions", "agy": ".gemini/antigravity",
        "opencode": ".local/share/opencode"}
_OFF = {"0", "false", "no", "off"}


def enabled(cli: str) -> bool:
    return os.environ.get(f"MEMORYMASTER_TRANSCRIPT_SOURCE_{cli.upper()}", "").strip().lower() not in _OFF


def roots_under(home: Path) -> dict[str, Path]:
    return {cli: home / rel for cli, rel in _REL.items()}


def default_root(cli: str) -> Path:
    raw = os.environ.get(f"MEMORYMASTER_TRANSCRIPT_ROOT_{cli.upper()}", "").strip()
    if raw:
        return Path(raw).expanduser()
    if cli == "opencode" and os.environ.get("XDG_DATA_HOME"):
        return Path(os.environ["XDG_DATA_HOME"]) / "opencode"
    return roots_under(Path.home())[cli]


def line_files(cli: str, root: Path) -> list[Path]:
    """Claude: ``<project>/<session>.jsonl`` (sub-agent transcripts live deeper); Codex: every rollout."""
    return sorted(path for path in root.glob("*/*.jsonl" if cli == "claude" else "**/*.jsonl") if path.is_file())


def codex_message(entry: dict, state: dict) -> tuple[str, str]:
    """``(role, text)`` of one Codex rollout record, ``("", "")`` for tool traffic, memory echoes and non-messages."""
    found = capture._conversation_record(entry, "codex", state=state)
    return (found[0], "\n".join(span.text for span in found[1])) if found else ("", "")


def codex_cwd(path: Path) -> str | None:
    """The session's working directory (``session_meta``); ``None`` when absent or when it is our own internal run."""
    cwd = capture._probe_session(path, "codex")[1]
    return None if capture._in_claude_cli_scratch(cwd) else cwd


@dataclass(frozen=True)
class Session:
    """One agy/OpenCode session; ``stamp`` is a cheap change marker (``None`` = always re-read)."""

    key: str
    stamp: tuple[int, int] | None
    load: Callable[[], tuple[str | None, list[tuple[str, str, str]]]]  # -> (cwd, [(timestamp, role, text)])


def _opencode_ids(root: Path) -> list[str]:
    database = root / (os.environ.get("OPENCODE_DB") or "opencode.db")
    if not database.is_file():
        return sorted(path.stem for path in (root / "storage" / "session").glob("*/*.json"))
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)) as conn:
        for table in ("session_v2", "session"):
            try:
                return [row[0] for row in conn.execute(f"SELECT id FROM {table} ORDER BY id")]
            except sqlite3.OperationalError:
                continue
    return []


def _opencode_load(root: Path, session_id: str):
    database = root / (os.environ.get("OPENCODE_DB") or "opencode.db")
    source = capture._opencode_sqlite(database, session_id) if database.is_file() else capture._opencode_legacy(root, session_id)
    if source is None:
        return None, []
    directory, records = source
    cwd = None if capture._in_claude_cli_scratch(directory) else directory or None
    return cwd, [(m.timestamp, m.role, m.text) for m in capture._opencode_messages(records)]


def _agy_load(session):
    cwd = read_executor_metadata(session.database_path).cwd
    cwd = None if capture._in_claude_cli_scratch(cwd) else cwd
    return cwd, [(m.timestamp, m.role, m.text) for m in extract_agy_session(session.transcript_path)]


def whole_sessions(cli: str, root: Path) -> list[Session]:
    """Sessions of an agy/OpenCode ``root``; ``[]`` when the root does not exist."""
    if not root.is_dir():
        return []
    if cli == "agy":
        out = []
        for session in discover_agy_sessions(root):
            files = [p for p in (session.transcript_path, session.full_path) if p]
            stats = [p.stat() for p in files]
            out.append(Session(session.uuid, (sum(s.st_size for s in stats), max(s.st_mtime_ns for s in stats)),
                               lambda session=session: _agy_load(session)))
        return out
    return [Session(sid, None, lambda sid=sid: _opencode_load(root, sid)) for sid in _opencode_ids(root)]
