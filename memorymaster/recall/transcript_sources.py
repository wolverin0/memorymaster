"""Antigravity (agy) and OpenCode readers for the raw-transcript index (T-2277, audit D-1).

Neither store is an append-only JSONL file, so they hand the index whole sessions: ``(directory, messages)``
with ``Msg.ordinal`` as the stable position (agy step index, OpenCode message ordinal).  Both reuse the
dreaming readers (``dreaming/agy_source.py``, ``dreaming/capture.py``) read-only; scope resolution,
fragmenting and persistence stay in ``transcript_index``.  The dreaming modules are imported lazily, so the
recall hook (which only searches) never pays for them.
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, NamedTuple

from memorymaster.core.security import sanitize_persisted_text
from memorymaster.recall.machine_traffic import is_transport_notice

MIN_MESSAGE_CHARS = 20
NOISE = re.compile(r"^(<local-command|<command-name>|<system-reminder>|\[MemoryMaster|Caveat:)", re.I)
# Harness-injected "user" turns of Codex and orchestrator traffic: not something a person said.
_HARNESS_NOISE = re.compile(
    r"^(<environment_context|# AGENTS\.md|<user_instructions|<subagent_notification|<codex_internal"
    r"|<turn_aborted|\[A2A from)", re.I)
_AGY_NOT_STATEMENTS = {"historical_error", "model_reasoning"}  # tool/error noise and raw reasoning, not statements
_OC_TABLES = (("message", "session_id"), ("part", "session_id"), ("session_message", "session_id"))


class Msg(NamedTuple):
    ordinal: int
    role: str
    text: str
    timestamp: str


class OcSession(NamedTuple):
    key: str  # changes whenever a message or part of the session changes
    size: int
    mtime_ms: int
    child: bool  # sub-agent session: skipped like a Claude sidechain
    directory: str = ""  # its workspace ("" = unknown: the legacy layout reports it only when the session is read)


# Workspaces of MemoryMaster's own provider calls (dreaming extraction through OpenCode / Claude).  The literals mirror
# dreaming/backfill.py (`"provider-slot-" in cwd or "dreaming-opencode" in cwd`, inline there) and
# dreaming/capture.py:_in_claude_cli_scratch; tests/test_transcript_index_sources.py pins the literals.
_GENERATED_MARKERS = ("provider-slot-", "dreaming-opencode")


def is_generated_cwd(cwd: object) -> bool:
    """True for a session run by MemoryMaster's own pipelines: it quotes prompts and answer keys, never a person."""
    if not isinstance(cwd, str) or not cwd:
        return False
    from memorymaster.dreaming.capture import _in_claude_cli_scratch

    return any(marker in cwd for marker in _GENERATED_MARKERS) or _in_claude_cli_scratch(cwd)


def usable_text(role: str, text: str) -> str:
    """The COMPLETE message sanitised (before fragmenting), or ``""`` when too short / harness or machine traffic."""
    text = text.strip()
    if len(text) < MIN_MESSAGE_CHARS or NOISE.match(text) or _HARNESS_NOISE.match(text):
        return ""
    if role == "user" and is_transport_notice(text):
        return ""
    return sanitize_persisted_text(text)[0]


# --- agy ---------------------------------------------------------------------------------------------------

def agy_dirs(root: Path) -> list[Path]:
    """Roots holding ``brain/``: ``root`` itself, else the agy product folders under it (``~/.gemini``)."""
    if (root / "brain").is_dir():
        return [root]
    return [root / name for name in ("antigravity-cli", "antigravity", "antigravity-ide") if (root / name / "brain").is_dir()]


def agy_sessions(root: Path) -> dict[str, Any]:
    from memorymaster.dreaming.agy_source import discover_agy_sessions

    found: dict[str, Any] = {}
    for base in agy_dirs(root):
        for session in discover_agy_sessions(base):
            found.setdefault(session.uuid, session)
    return found


def agy_stamp(session: Any) -> tuple[int, int]:
    """``(size, mtime_ns)`` of the transcript; the companion db (it holds the workspace) counts toward the mtime."""
    st = session.transcript_path.stat()
    mtime = st.st_mtime_ns
    if session.database_path is not None:
        try:
            mtime = max(mtime, session.database_path.stat().st_mtime_ns)
        except OSError:
            pass
    return st.st_size, mtime


class AgyRead(NamedTuple):
    scope: str  # "" = no resolvable workspace
    automated: bool
    messages: list[Msg]


_AGY_OWN_DIRS = {".gemini", "antigravity", "antigravity-cli", "antigravity-ide", "brain"}  # agy's own files, not the project
_MAX_TOUCHED_PATHS = 4000
_MIN_ATTRIBUTABLE_FILES = 3  # a "majority" of one or two files is an accident, not evidence of a workspace


def _agy_touched_scope(database: Path | None, scope_of: Callable[[str], str]) -> tuple[str, bool]:
    """Workspace of a session whose executor metadata carries no workspace field (newer agy): the project most
    of the files the session touched belong to (executor_metadata field path 18.1.3 lists them).  At least 3
    attributable files and a strict majority of them are required, so a session straying into other projects is not
    attributed to one of them by accident.  Returns ``(scope, automated)``."""
    from memorymaster.dreaming.agy_source import MAX_METADATA_BYTES, MAX_METADATA_ROWS, _fields

    if database is None or not database.is_file():
        return "", False
    files: set[str] = set()
    try:
        with closing(_ro(database)) as conn:
            for (blob,) in conn.execute(
                "SELECT data FROM executor_metadata WHERE typeof(data) = 'blob' AND length(data) <= ? LIMIT ?",
                (MAX_METADATA_BYTES, MAX_METADATA_ROWS),
            ):
                try:
                    # not _decode_path: its 64-value fan-out bound rejects every real blob here (field 18 repeats)
                    files.update(
                        item.decode("utf-8") for outer in _fields(blob).get(18, []) for inner in _fields(outer).get(1, [])
                        for item in _fields(inner).get(3, []) if len(item) <= 4096
                    )
                except (ValueError, UnicodeError):
                    continue
                if len(files) >= _MAX_TOUCHED_PATHS:
                    break
    except sqlite3.Error:
        return "", False
    parents: Counter[str] = Counter()
    for raw in files:
        pure = PureWindowsPath(raw) if PureWindowsPath(raw).drive else PurePosixPath(raw)
        if pure.is_absolute() and not _AGY_OWN_DIRS & {part.lower() for part in pure.parts}:
            parents[str(pure.parent)] += 1
    if sum(parents[p] for p in parents if is_generated_cwd(p)) * 2 > sum(parents.values()):
        return "", True
    by_scope: Counter[str] = Counter()
    for parent, count in parents.items():
        scope = scope_of(parent)
        if scope not in ("", "global", "user"):
            by_scope[scope] += count
    if not by_scope:
        return "", False
    scope, count = by_scope.most_common(1)[0]
    total = sum(by_scope.values())
    return (scope, False) if total >= _MIN_ATTRIBUTABLE_FILES and count * 2 > total else ("", False)


def agy_session(session: Any, scope_of: Callable[[str], str], include_automated: bool = False) -> AgyRead:
    """One session: its workspace scope (old metadata field, else the touched-files majority) and messages.  A
    session without a resolvable workspace, or one run by our own pipelines, is not read at all."""
    from memorymaster.dreaming.agy_source import iter_agy_records, read_executor_metadata

    meta = read_executor_metadata(session.database_path)
    if meta.status == "resolved":
        scope, automated = scope_of(meta.cwd), is_generated_cwd(meta.cwd)
    else:
        scope, automated = _agy_touched_scope(session.database_path, scope_of)
    if not scope or (automated and not include_automated):
        return AgyRead(scope, automated and not include_automated, [])
    size = session.transcript_path.stat().st_size
    full_end = session.full_path.stat().st_size if session.full_path else 0
    messages: list[Msg] = []
    for record in iter_agy_records(session.transcript_path, frozen_end=size, full_frozen_end=full_end):
        if _AGY_NOT_STATEMENTS & set(record.provenance):
            continue
        text = usable_text(record.message.role, record.message.text)
        if text:
            messages.append(Msg(record.step_index, record.message.role, text, record.message.timestamp))
    return AgyRead(scope, False, messages)


# --- OpenCode ----------------------------------------------------------------------------------------------

def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)  # never wait long on the producer
    conn.execute("PRAGMA query_only=ON")
    return conn


def opencode_sessions(root: Path) -> dict[str, OcSession]:
    """Every OpenCode session with a change key.  Raises ``sqlite3.Error`` (e.g. locked): the caller skips the round."""
    database = root / "opencode.db"
    if not database.is_file():
        return _opencode_legacy_sessions(root)
    sessions: dict[str, OcSession] = {}
    with closing(_ro(database)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        children: dict[str, tuple[bool, str]] = {}
        for table in ("session", "session_v2"):
            if table in tables:
                for sid, parent, directory in conn.execute(f"SELECT id, parent_id, directory FROM {table}"):
                    children.setdefault(sid, (bool(parent), str(directory or "")))
        stamps: dict[str, list[tuple[str, int, int]]] = {sid: [] for sid in children}
        for table, column in _OC_TABLES:
            if table in tables:
                for sid, count, newest in conn.execute(
                    f"SELECT {column}, count(*), max(time_updated) FROM {table} GROUP BY {column}"
                ):
                    if sid in stamps:
                        stamps[sid].append((table, count, newest or 0))
        for sid, (child, directory) in children.items():
            parts = stamps[sid]
            sessions[sid] = OcSession("|".join(f"{t}:{c}:{n}" for t, c, n in parts), sum(c for _, c, _ in parts),
                                      max((n for _, _, n in parts), default=0), child, directory)
    return sessions


def _opencode_legacy_sessions(root: Path) -> dict[str, OcSession]:
    # shortcut: the frozen pre-database JSON layout; parts and sub-agent links are not part of the key, since
    # OpenCode migrated this layout away and nothing writes it any more.
    storage = root / "storage"
    sessions: dict[str, OcSession] = {}
    for path in storage.glob("session/*/*.json"):
        files = [path, *(storage / "message" / path.stem).glob("*.json")]
        stamps = [(f.name, f.stat().st_mtime_ns) for f in files]
        sessions[path.stem] = OcSession(
            "|".join(f"{n}:{m}" for n, m in sorted(stamps)), len(files), max(m for _, m in stamps) // 1_000_000, False,
        )
    return sessions


def opencode_session(root: Path, session_id: str) -> tuple[str | None, list[Msg]]:
    """``(directory, messages)`` of one session via the dreaming reader (read-only, bounded, one WAL snapshot)."""
    from memorymaster.dreaming.capture import _opencode_legacy, _opencode_messages, _opencode_sqlite

    database = root / "opencode.db"
    source = _opencode_sqlite(database, session_id) if database.is_file() else _opencode_legacy(root, session_id)
    if source is None:
        return None, []
    directory, records = source
    messages: list[Msg] = []
    for ordinal, message in enumerate(_opencode_messages(records)):
        text = usable_text(message.role, message.text)
        if text:
            messages.append(Msg(ordinal, message.role, text, message.timestamp))
    return directory or None, messages


def is_unreadable_agy(exc: Exception) -> bool:
    """The agy reader refused the data itself (``AgySourceError``: a malformed or oversized record): a property of
    the file, so it is recorded with its stamp and retried only when the file changes."""
    from memorymaster.dreaming.agy_source import AgySourceError

    return isinstance(exc, AgySourceError)


def is_oversized(exc: Exception) -> bool:
    """The dreaming reader's size bounds (``oversized OpenCode session/record/message``): a property of the data."""
    return isinstance(exc, ValueError) and "oversized" in str(exc)


def is_locked(exc: sqlite3.Error) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and any(w in str(exc).lower() for w in ("locked", "busy"))


__all__ = ["AgyRead", "Msg", "OcSession", "agy_session", "agy_sessions", "agy_stamp", "is_generated_cwd",
           "is_locked", "is_oversized", "is_unreadable_agy", "opencode_session", "opencode_sessions", "usable_text"]
