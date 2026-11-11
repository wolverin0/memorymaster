"""Where each CLI keeps its sessions and how a record becomes ``(role, text)`` for the transcript index (T-2277).

One parser per format: Codex rollouts, agy exports and OpenCode stores are read by the dreaming adapters
(``dreaming/capture.py``, ``dreaming/agy_source.py``); this module only discovers sessions and hands their
messages to ``recall/transcript_index.py``, which sanitises, scopes and chunks them like Claude's.  Every source
has a kill switch, ``MEMORYMASTER_TRANSCRIPT_SOURCE_<CLI>=0``, and a root override, ``MEMORYMASTER_TRANSCRIPT_ROOT_<CLI>``.
"""
from __future__ import annotations

import json
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


CLAUDE_AUTOMATED_ENTRYPOINT = "sdk-cli"  # headless `claude -p` / SDK runs tag every record with it
CODEX_AUTOMATED = {"codex_exec", "exec", "subagent"}  # session_meta originator / source of headless and sub-agent runs


def claude_automated(record: dict) -> bool:
    return record.get("entrypoint") == CLAUDE_AUTOMATED_ENTRYPOINT


def codex_automated(meta: dict) -> bool:
    """``session_meta`` of a ``codex exec`` run or a sub-agent: its transcript quotes prompts and answer keys."""
    source = meta.get("source")
    names = {str(meta.get("originator", "")).lower()}
    names |= {str(key).lower() for key in source} if isinstance(source, dict) else {str(source).lower()}
    return bool(names & CODEX_AUTOMATED)


def codex_meta(path: Path) -> tuple[str | None, bool]:
    """``(cwd, automated)`` from the rollout's ``session_meta``; ``cwd`` is ``None`` when absent or when it is our own
    internal run (not attributable)."""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for _, raw in zip(range(capture._PROBE_MAX_LINES), handle):
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            payload = entry.get("payload") if isinstance(entry, dict) and entry.get("type") == "session_meta" else None
            if isinstance(payload, dict):
                cwd = str(payload.get("cwd") or "") or None
                return (None if capture._in_claude_cli_scratch(cwd) else cwd), codex_automated(payload)
    return None, False


@dataclass(frozen=True)
class Session:
    """One agy/OpenCode session; ``stamp`` is a cheap change marker (``None`` = always re-read)."""

    key: str
    stamp: tuple[int, int] | None
    load: Callable[[], tuple[str | None, list[tuple[str, str, str]]]]  # -> (cwd, [(timestamp, role, text)])


def _opencode_ids(root: Path) -> list[tuple[str, tuple[int, int] | None]]:
    """``(session id, stamp)``; the stamp is ``(0, time_updated in ns)`` so the cutoff and caps apply without
    reading any message.  A locked or unreadable store raises (the caller keeps its rows); only a missing table or
    column falls through."""
    database = root / (os.environ.get("OPENCODE_DB") or "opencode.db")
    if not database.is_file():
        found = []
        for path in sorted((root / "storage" / "session").glob("*/*.json")):
            try:
                found.append((path.stem, (0, path.stat().st_mtime_ns)))
            except OSError:
                found.append((path.stem, None))
        return found
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)) as conn:
        for table in ("session_v2", "session"):
            for sql, stamped in ((f"SELECT id, time_updated FROM {table} ORDER BY id", True),
                                 (f"SELECT id FROM {table} ORDER BY id", False)):
                try:
                    rows = conn.execute(sql).fetchall()
                except sqlite3.OperationalError as exc:
                    if "no such" not in str(exc):
                        raise  # locked / busy: the source is unavailable, not empty
                    continue
                return [(r[0], (0, int(r[1]) * 1_000_000) if stamped and r[1] is not None else None) for r in rows]
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
            try:
                stats = [p.stat() for p in files]
                stamp = (sum(s.st_size for s in stats), max(s.st_mtime_ns for s in stats))
            except OSError:  # a file vanished or is locked mid-scan: re-read it (a failure is counted, rows are kept)
                stamp = None
            out.append(Session(session.uuid, stamp, lambda session=session: _agy_load(session)))
        return out
    return [Session(sid, stamp, lambda sid=sid: _opencode_load(root, sid)) for sid, stamp in _opencode_ids(root)]
