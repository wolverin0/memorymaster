"""Opt-in raw-transcript fallback: a dated FTS5 index over Claude Code transcripts.

When claims do not answer a question, the operator's own raw local transcripts
often do (T-1817: claims alone 12.5% correct, claims + raw union 30%).  This
module keeps a *sidecar* SQLite file (default ``~/.memorymaster/transcripts.db``,
never the authoritative claims DB) with one row per transcript fragment, each
carrying an ISO date, the session id and the project scope.  It never creates
claims, and nothing runs unless the operator calls ``transcript-index`` /
``transcript-search`` explicitly.

Design
------
* Sources (all read-only, each independently failing; see ``SOURCES``): Claude
  ``~/.claude/projects/*/*.jsonl``, Codex ``~/.codex/sessions/Y/M/D/rollout-*.jsonl``, Antigravity
  ``~/.gemini/antigravity*/brain/<uuid>/.system_generated/logs`` and OpenCode ``opencode.db``
  (``transcript_sources.py``; T-2277).  Only user/assistant text is indexed; tool calls/results,
  sidechains/sub-agents, headless runs and harness noise are skipped.  A fragment records its ``source``
  (added to an existing index by ``ALTER TABLE``, so no rebuild); agy/OpenCode have no JSONL lines, so their
  ``first_line``/``last_line`` are step/message ordinals and their sessions are re-read whole when they change.
* Incremental: per-file ``(size, mtime_ns, byte offset, line count, prefix
  sha256)`` (JSONL sources; OpenCode keys on its message/part change stamps).  Files are append-only, so a grown file is parsed from the stored
  offset *if* its indexed prefix still hashes to the stored digest; an
  unchanged one is skipped; a shrunk or rewritten one is re-indexed from
  scratch, and the fragments of a transcript that no longer exists are dropped.
* Every message is sanitised whole (``core.security.sanitize_persisted_text``)
  BEFORE it is cut into fragments and before anything is persisted, so a
  credential straddling a fragment edge cannot survive, and the sidecar never
  holds a raw secret.  Long messages are segmented into several fragments, never
  truncated.  Scope is resolved per record from that record's own ``cwd``;
  a record without one is not attributable and is not indexed.
* ``PRAGMA user_version`` marks the format.  An index without the current
  marker (built by the earlier unsanitised code) is never served by
  :func:`search` and is dropped and rebuilt by :func:`index_transcripts`
  (``memorymaster transcript-index``).
* Deployment: the indexer and the hooks that search it must be the same version.  An older indexer (it only
  knows Claude) deletes every non-Claude row on its next run; this one re-parses them on its next run.
* Session id = ``sha256(<project-dir>/<file>)[:16]`` (other sources hash ``<source>:<id or path>``) -- opaque (no username
  leaks from the encoded project directory) and identical to the T-1817 bench
  transcript id.  Fragments store the 0-based JSONL line range they cover.
* Ranking: FTS5 BM25 relevance first; among results whose relevance is within
  one band (``RELEVANCE_BAND``) of each other, newest date first.  Every hit
  carries its date so a stale conclusion is recognisable.
* Display text is sanitised once more on the way out (defence in depth).
* No embedding model: FTS5 only, so indexing is one cheap process.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple

from memorymaster.core.scope_utils import scope_from_cwd
from memorymaster.core.security import sanitize_persisted_text
from memorymaster.recall import transcript_sources as ts
from memorymaster.recall.recall_tokenizer import _candidate_tokens, centered_excerpt
from memorymaster.stores._storage_shared import connect_ro, open_conn

logger = logging.getLogger(__name__)

INDEX_ENV = "MEMORYMASTER_TRANSCRIPT_INDEX"
ROOT_ENV = "MEMORYMASTER_TRANSCRIPT_ROOT"  # the Claude root (unchanged meaning); one more variable per source:
ROOT_ENVS = {
    "claude": ROOT_ENV,
    "codex": "MEMORYMASTER_TRANSCRIPT_ROOT_CODEX",
    "agy": "MEMORYMASTER_TRANSCRIPT_ROOT_AGY",
    "opencode": "MEMORYMASTER_TRANSCRIPT_ROOT_OPENCODE",
}
SOURCES = tuple(ROOT_ENVS)
OFF_VALUES = {"off", "none", "0", "false", "no", "disabled"}  # a root variable set to one of these switches the source off

FORMAT_VERSION = 1  # PRAGMA user_version; 0 = built before sanitise-on-write (T-1876)
CHUNK_CHARS = 1800  # fragment size target (characters of "role: text")
MIN_MESSAGE_CHARS = ts.MIN_MESSAGE_CHARS
RELEVANCE_BAND = 0.05  # relevance fraction within which recency breaks ties
EXCERPT_CHARS = 600
MAX_QUERY_TOKENS = 12
# Headless `claude -p` / SDK runs (extraction, steward, benchmark harnesses) tag every
# record with this entrypoint; their transcripts quote prompts and answer keys.
AUTOMATED_ENTRYPOINT = "sdk-cli"

_NOISE = ts.NOISE

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    rel TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'claude',
    state TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    offset INTEGER NOT NULL,
    lines INTEGER NOT NULL,
    prefix_sha TEXT NOT NULL,
    automated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fragments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'claude',
    scope TEXT NOT NULL,
    date TEXT NOT NULL,
    first_line INTEGER NOT NULL,
    last_line INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fragments_session ON fragments(session_id);
CREATE VIRTUAL TABLE IF NOT EXISTS fragments_fts USING fts5(
    text, content='fragments', content_rowid='id', tokenize='porter unicode61'
);
"""


@dataclass(frozen=True)
class TranscriptHit:
    """One cited fragment returned by :func:`search`."""

    date: str
    session_id: str
    scope: str
    first_line: int
    last_line: int
    score: float
    excerpt: str
    source: str = "claude"

    def to_dict(self) -> dict:
        return asdict(self)


def default_index_path() -> Path:
    raw = os.environ.get(INDEX_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".memorymaster" / "transcripts.db"


def default_root() -> Path:
    raw = os.environ.get(ROOT_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".claude" / "projects"


def source_root(name: str) -> Path | None:
    """The root of one source (its env variable, else the CLI's default home location); ``None`` = switched off."""
    raw = os.environ.get(ROOT_ENVS[name], "").strip()
    if raw.lower() in OFF_VALUES:
        return None
    if name == "claude":
        return default_root()
    if raw:
        return Path(raw).expanduser()
    home = Path.home()
    return {
        "codex": home / ".codex" / "sessions",
        "agy": home / ".gemini",
        "opencode": Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "opencode",
    }[name]


def session_id_for(root: Path, path: Path, source: str = "claude") -> str:
    rel = path.relative_to(root).as_posix()
    return hashlib.sha256((rel if source == "claude" else f"{source}:{rel}").encode()).hexdigest()[:16]


def _opaque_id(source: str, ident: str) -> str:
    return hashlib.sha256(f"{source}:{ident}".encode()).hexdigest()[:16]


_ADDED_COLUMNS = (
    ("files", "source", "TEXT NOT NULL DEFAULT 'claude'"),
    ("files", "state", "TEXT NOT NULL DEFAULT ''"),
    ("fragments", "source", "TEXT NOT NULL DEFAULT 'claude'"),
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _connect(index_path: Path) -> tuple[sqlite3.Connection, bool]:
    """Open the sidecar for writing; ``(conn, rebuilt)``.

    An index whose ``user_version`` is not :data:`FORMAT_VERSION` was built by the earlier unsanitised
    code: its tables are dropped (with ``secure_delete`` and a ``VACUUM``, so the raw rows do not linger in
    free pages) and recreated empty.  The sidecar is derived data; the caller re-indexes from the transcripts.
    """
    index_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_conn(index_path)
    conn.execute("PRAGMA secure_delete = ON")
    rebuilt = False
    has_tables = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'files'").fetchone() is not None
    if has_tables and conn.execute("PRAGMA user_version").fetchone()[0] != FORMAT_VERSION:
        conn.executescript("DROP TABLE IF EXISTS fragments_fts; DROP TABLE IF EXISTS fragments; DROP TABLE IF EXISTS files;")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        rebuilt = True
    elif has_tables and "automated" not in _columns(conn, "files"):
        # T-2077 L28: additive, so a current-format index is not made unservable. Sessions that stored no
        # fragment are treated as skipped-automated once (a tool-only session just gets re-parsed, harmlessly).
        conn.execute("ALTER TABLE files ADD COLUMN automated INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE files SET automated = 1 WHERE session_id NOT IN (SELECT DISTINCT session_id FROM fragments)")
        conn.commit()
    if has_tables and not rebuilt:
        # T-2277: additive too (the rows already in the index are all Claude's, which is the column default)
        for table, column, ddl in _ADDED_COLUMNS:
            if column not in _columns(conn, table):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.commit()
    conn.executescript(_SCHEMA)
    conn.execute(f"PRAGMA user_version = {int(FORMAT_VERSION)}")
    return conn, rebuilt


def index_status(index_path: Path | None = None) -> str:
    """``missing`` | ``ok`` | ``outdated`` (built by older code: rebuild it) | ``unreadable``."""
    index_path = index_path or default_index_path()
    if not index_path.is_file():
        return "missing"
    try:
        with closing(connect_ro(index_path.resolve().as_posix())) as conn:
            return "ok" if conn.execute("PRAGMA user_version").fetchone()[0] == FORMAT_VERSION else "outdated"
    except sqlite3.Error:
        return "unreadable"


def _message_text(record: dict) -> tuple[str, str]:
    """``(role, text)`` for a user/assistant record, ``("", "")`` otherwise."""
    role = record.get("type")
    if role not in ("user", "assistant") or record.get("isSidechain"):
        return "", ""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else record.get("content")
    if isinstance(content, list):
        content = "\n".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    if not isinstance(content, str):
        return "", ""
    text = content.strip()
    if len(text) < MIN_MESSAGE_CHARS or _NOISE.match(text):
        return "", ""
    # The COMPLETE message is sanitised here, before segmentation and persistence (T-1876 N1).
    return role, sanitize_persisted_text(text)[0]


def _iso_date(value: object, fallback: str) -> str:
    """Normalise a record timestamp to ``YYYY-MM-DDTHH:MM:SSZ`` (UTC)."""
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return fallback
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return fallback


def _mtime_iso(mtime_ns: int) -> str:
    return datetime.fromtimestamp(mtime_ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pieces(text: str) -> list[str]:
    """Cut a complete message into pieces of at most :data:`CHUNK_CHARS`, preferring a whitespace edge."""
    pieces: list[str] = []
    start = 0
    floor = CHUNK_CHARS * 4 // 5
    while start < len(text):
        end = start + CHUNK_CHARS
        if end < len(text):
            cut = max(text.rfind(" ", start + floor, end), text.rfind("\n", start + floor, end))
            end = cut + 1 if cut > start else end
        pieces.append(text[start:end])
        start = end
    return pieces


def _verified_prefix(path: Path, length: int, expected: str):
    """The sha256 state of the first ``length`` bytes of ``path`` if it matches ``expected``, else ``None``.

    shortcut: re-reads the whole indexed prefix of a changed file (sequential, cheap next to parsing);
    upgrade to a head+tail window if multi-hundred-MB transcripts become common.
    """
    hasher = hashlib.sha256()
    remaining = length
    with path.open("rb") as fh:
        while remaining > 0:
            block = fh.read(min(remaining, 1 << 20))
            if not block:
                return None  # shorter than what was indexed
            hasher.update(block)
            remaining -= len(block)
    return hasher if hasher.hexdigest() == expected else None


def _scope_for_cwd(cwd: object, cache: dict[str, str]) -> str:
    """The scope of one record's ``cwd``; ``""`` when the record carries none (not attributable)."""
    if not isinstance(cwd, str) or not cwd.strip():
        return ""
    if cwd not in cache:
        try:
            cache[cwd] = scope_from_cwd(cwd)
        except (OSError, ValueError, RuntimeError):
            cache[cwd] = ""
    return cache[cwd]


class _Packer:
    """Cuts whole sanitised messages into fragments: at most :data:`CHUNK_CHARS` each, never spanning two scopes."""

    def __init__(self) -> None:
        self.fragments: list[tuple[str, str, int, int, str]] = []  # (scope, date, first, last, text)
        self._buf: list[str] = []
        self._size = 0
        self._first = self._last = 0
        self._date = self._scope = ""

    def _flush(self) -> None:
        if self._buf:
            self.fragments.append((self._scope, self._date, self._first, self._last, "\n".join(self._buf)))
        self._buf, self._size = [], 0

    def add(self, role: str, text: str, scope: str, date: str, line: int) -> None:
        if self._buf and scope != self._scope:
            self._flush()
        for piece in _pieces(text):
            entry = f"{role}: {piece}"
            if self._buf and self._size + len(entry) > CHUNK_CHARS:
                self._flush()
            if not self._buf:
                self._first, self._date, self._scope = line, date, scope
            self._buf.append(entry)
            self._last = line
            self._size += len(entry)

    def done(self) -> list[tuple[str, str, int, int, str]]:
        self._flush()
        return self.fragments


class _Dialect(NamedTuple):
    """How one append-only JSONL source says who spoke, what, and in which workspace."""

    automated: Callable[[dict], bool]
    # (record, per-file state, scope cache) -> (role, sanitised text, scope); text/scope "" = not indexable
    read: Callable[[dict, dict, dict[str, str]], tuple[str, str, str]]


def _claude_read(record: dict, state: dict, scopes: dict[str, str]) -> tuple[str, str, str]:
    role, text = _message_text(record)
    return role, text, _scope_for_cwd(record.get("cwd"), scopes) if text else ""


@lru_cache(maxsize=1)
def _conversation_record() -> Callable[..., Any]:
    # the dreaming capture's Codex normaliser (tool results, memory-tool echoes, role mapping); imported on first use
    from memorymaster.dreaming.capture import _conversation_record as record

    return record


def _codex_read(record: dict, state: dict, scopes: dict[str, str]) -> tuple[str, str, str]:
    payload = record.get("payload")
    if record.get("type") in ("session_meta", "turn_context") and isinstance(payload, dict):
        if payload.get("cwd"):
            state["scope"] = _scope_for_cwd(payload["cwd"], scopes)  # the scope, never the raw path, is what persists
        return "", "", ""
    found = _conversation_record()(record, "codex", state=state.setdefault("mm", {}))
    if not found:
        return "", "", ""
    role, spans = found
    text = ts.usable_text(role, "\n".join(span.text for span in spans))
    return role, text, state.get("scope", "") if text else ""


def _codex_automated(record: dict) -> bool:
    payload = record.get("payload")
    return (record.get("type") == "session_meta" and isinstance(payload, dict)
            and (payload.get("originator") == "codex_exec" or isinstance(payload.get("source"), dict)
                 or ts.is_generated_cwd(payload.get("cwd"))))


_DIALECTS = {
    "claude": _Dialect(
        lambda record: record.get("entrypoint") == AUTOMATED_ENTRYPOINT or ts.is_generated_cwd(record.get("cwd")),
        _claude_read),
    "codex": _Dialect(_codex_automated, _codex_read),
}


def _parse_increment(path: Path, offset: int, line_no: int, fallback_date: str, skip_automated: bool,
                     hasher, scopes: dict[str, str], dialect: _Dialect = _DIALECTS["claude"],
                     state: dict | None = None):
    """Parse complete lines from ``offset``; return ``(fragments, new_offset, new_lines, automated)``.

    ``hasher`` (the sha256 state of the bytes before ``offset``) is advanced over every consumed line.
    ``automated`` is True (and fragments empty) when ``skip_automated`` and the ``dialect`` flags a record
    as a headless session; the caller records the file as seen.  ``state`` carries what a dialect learns from
    earlier lines (the Codex workspace) in and out.

    Fragments are ``(scope, date, first_line, last_line, text)``; scope comes from the dialect (Claude: each
    record's own ``cwd``) and a fragment never spans two scopes.  An unterminated trailing line is left for
    the next run.
    """
    state = {} if state is None else state
    packer = _Packer()
    with path.open("rb") as fh:
        fh.seek(offset)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break  # unterminated (even if it parses): re-read next time, so line numbers stay put
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                record = None
            offset += len(raw)
            hasher.update(raw)
            this_line = line_no
            line_no += 1
            if not isinstance(record, dict):
                continue
            state["records"] = state.get("records", 0) + 1
            if skip_automated and dialect.automated(record):
                return [], offset, line_no, True
            role, text, scope = dialect.read(record, state, scopes)
            if text and scope:
                packer.add(role, text, scope, _iso_date(record.get("timestamp"), fallback_date), this_line)
    return packer.done(), offset, line_no, False


def _delete_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute(
        "INSERT INTO fragments_fts(fragments_fts, rowid, text) "
        "SELECT 'delete', id, text FROM fragments WHERE session_id = ?",
        (session_id,),
    )
    conn.execute("DELETE FROM fragments WHERE session_id = ?", (session_id,))


def _write_session(conn: sqlite3.Connection, source: str, rel: str, sid: str, frags: list, *, replace: bool,
                   size: int, mtime_ns: int, offset: int = 0, lines: int = 0, prefix_sha: str = "",
                   automated: bool = False, state: str = "") -> None:
    with conn:
        if replace:
            _delete_session(conn, sid)
        for scope, date, first, last, text in frags:
            cur = conn.execute(
                "INSERT INTO fragments(session_id, source, scope, date, first_line, last_line, text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, source, scope, date, first, last, text),
            )
            conn.execute("INSERT INTO fragments_fts(rowid, text) VALUES (?, ?)", (cur.lastrowid, text))
        conn.execute(
            "INSERT OR REPLACE INTO files(rel, source, state, session_id, size, mtime_ns, offset, lines, prefix_sha, "
            "automated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rel, source, state, sid, size, mtime_ns, offset, lines, prefix_sha, int(automated)),
        )


def _drop_gone(conn: sqlite3.Connection, stats: dict[str, int], source: str, existing: set[str]) -> None:
    """A session that no longer exists must stop being searchable.  ``existing`` spans the whole source, so
    --since-days / --max-files never read as a deletion."""
    for gone in [r for r in conn.execute("SELECT rel, session_id FROM files WHERE source = ?", (source,)).fetchall()
                 if r["rel"] not in existing]:
        with conn:
            _delete_session(conn, gone["session_id"])
            conn.execute("DELETE FROM files WHERE rel = ?", (gone["rel"],))
        stats["files_removed"] += 1


_PER_SOURCE = ("files_seen", "files_indexed", "errors")


def _tick(stats: dict[str, int], source: str, key: str) -> None:
    stats[key] += 1
    if key in _PER_SOURCE:
        stats[f"{source}_{key}"] += 1
    elif key.startswith("skipped_"):
        stats[f"{source}_{key}"] = stats.get(f"{source}_{key}", 0) + 1


def _failed(stats: dict[str, int], source: str) -> None:
    """The source as a whole could not be read this run (as opposed to one session in it)."""
    stats["sources_failed"] += 1
    stats[f"{source}_failed"] = 1


class _Run(NamedTuple):
    conn: sqlite3.Connection
    stats: dict[str, int]
    cutoff: float | None
    max_files: int | None
    include_automated: bool
    scopes: dict[str, str]

    def full(self, source: str) -> bool:
        return self.max_files is not None and self.stats[f"{source}_files_indexed"] >= self.max_files


def _index_jsonl(run: _Run, source: str, root: Path, paths: list[Path]) -> None:
    """Incremental scan of an append-only JSONL source (Claude projects, Codex rollouts)."""
    conn, stats = run.conn, run.stats
    dialect = _DIALECTS[source]
    prefix = "" if source == "claude" else f"{source}:"
    _drop_gone(conn, stats, source, {prefix + path.relative_to(root).as_posix() for path in paths})
    for path in paths:
        if run.full(source):
            break
        try:
            st = path.stat()
        except OSError:
            _tick(stats, source, "errors")
            continue
        if run.cutoff is not None and st.st_mtime < run.cutoff:
            continue
        _tick(stats, source, "files_seen")
        rel = prefix + path.relative_to(root).as_posix()
        sid = session_id_for(root, path, source)
        row = conn.execute("SELECT * FROM files WHERE rel = ?", (rel,)).fetchone()
        # a session skipped as automated stored no fragments: --include-automated must re-parse it, and an
        # appended one is re-read from the start (its marker is in the head, not the tail)
        was_automated = bool(row and row["automated"])
        if (row and row["size"] == st.st_size and row["mtime_ns"] == st.st_mtime_ns
                and not (was_automated and run.include_automated)):
            stats["files_skipped"] += 1
            continue
        offset, line_no = 0, 0
        state: dict = {}
        try:
            resumable = row and st.st_size >= row["offset"] and not was_automated
            hasher = _verified_prefix(path, row["offset"], row["prefix_sha"]) if resumable else None
            if hasher is not None:
                offset, line_no, state = row["offset"], row["lines"], {"scope": row["state"]}
            else:
                hasher = hashlib.sha256()  # new, shrunk or rewritten file: parse from the start
            frags, new_offset, new_lines, automated = _parse_increment(
                path, offset, line_no, _mtime_iso(st.st_mtime_ns), not run.include_automated, hasher, run.scopes,
                dialect, state,
            )
        except OSError as exc:
            logger.warning("transcript_index: cannot read %s: %s", rel, exc)
            _tick(stats, source, "errors")
            continue
        if offset == 0 and new_lines and not state.get("records"):
            _tick(stats, source, "errors")  # complete lines but not one JSON object: not a transcript of this source
        _write_session(conn, source, rel, sid, frags, replace=bool(row) and offset == 0, size=st.st_size,
                       mtime_ns=st.st_mtime_ns, offset=new_offset, lines=new_lines, prefix_sha=hasher.hexdigest(),
                       automated=automated, state=state.get("scope", ""))
        if automated:
            stats["files_automated"] += 1
            continue
        if source != "claude" and not state.get("scope"):
            _tick(stats, source, "skipped_no_workspace")  # not attributable: stored nothing
            continue
        _tick(stats, source, "files_indexed")
        stats["fragments_added"] += len(frags)


def _pack(messages: list[ts.Msg], scope: str, fallback_date: str) -> list[tuple[str, str, int, int, str]]:
    packer = _Packer()
    for message in messages if scope else ():  # a session without a resolvable workspace is not attributable
        packer.add(message.role, message.text, scope, _iso_date(message.timestamp, fallback_date), message.ordinal)
    return packer.done()


def _index_agy(run: _Run, root: Path) -> None:
    """Antigravity: a session is re-read whole when its transcript (or its workspace companion db) changed."""
    conn, stats = run.conn, run.stats
    found = ts.agy_sessions(root)
    _drop_gone(conn, stats, "agy", {f"agy:{uuid}" for uuid in found})
    for uuid, session in sorted(found.items()):
        if run.full("agy"):
            break
        try:
            size, mtime_ns = ts.agy_stamp(session)
        except OSError:
            _tick(stats, "agy", "errors")
            continue
        if run.cutoff is not None and mtime_ns / 1e9 < run.cutoff:
            continue
        _tick(stats, "agy", "files_seen")
        rel = f"agy:{uuid}"
        row = conn.execute("SELECT size, mtime_ns, automated FROM files WHERE rel = ?", (rel,)).fetchone()
        if row and row["size"] == size and row["mtime_ns"] == mtime_ns and not (row["automated"] and run.include_automated):
            stats["files_skipped"] += 1
            continue
        try:
            read = ts.agy_session(session, lambda cwd: _scope_for_cwd(cwd, run.scopes), run.include_automated)
        except (OSError, ValueError, UnicodeError) as exc:  # AgySourceError is a ValueError
            if ts.is_unreadable_agy(exc):  # the file itself is refused: remember it under this stamp
                _write_session(conn, "agy", rel, _opaque_id("agy", uuid), [], replace=bool(row), size=size, mtime_ns=mtime_ns)
                _tick(stats, "agy", "skipped_unreadable")
            else:
                logger.warning("transcript_index: cannot read an agy session: %s", type(exc).__name__)
                _tick(stats, "agy", "errors")
            continue
        frags = _pack(read.messages, read.scope, _mtime_iso(mtime_ns))
        _write_session(conn, "agy", rel, _opaque_id("agy", uuid), frags, replace=bool(row), size=size, mtime_ns=mtime_ns,
                       automated=read.automated)
        if read.automated:
            stats["files_automated"] += 1
        elif not read.scope:
            _tick(stats, "agy", "skipped_no_workspace")
        else:
            _tick(stats, "agy", "files_indexed")
            stats["fragments_added"] += len(frags)


def _index_opencode(run: _Run, root: Path) -> None:
    """OpenCode: the incremental key of a session is its message/part change stamp (there is no append-only file)."""
    conn, stats = run.conn, run.stats
    try:
        found = ts.opencode_sessions(root)  # a locked/unreadable database skips the whole round
    except sqlite3.Error as exc:
        logger.warning("transcript_index: opencode database unavailable: %s", type(exc).__name__)
        _tick(stats, "opencode", "errors")
        _failed(stats, "opencode")
        return
    _drop_gone(conn, stats, "opencode", {f"opencode:{sid}" for sid, info in found.items() if not info.child})
    for sid, info in sorted(found.items()):
        if info.child:
            continue
        if run.full("opencode"):
            break
        if run.cutoff is not None and info.mtime_ms / 1000 < run.cutoff:
            continue
        _tick(stats, "opencode", "files_seen")
        rel = f"opencode:{sid}"
        row = conn.execute("SELECT state, automated FROM files WHERE rel = ?", (rel,)).fetchone()
        if row and row["state"] == info.key and not (row["automated"] and run.include_automated):
            stats["files_skipped"] += 1
            continue
        automated = ts.is_generated_cwd(info.directory) and not run.include_automated
        scope = _scope_for_cwd(info.directory, run.scopes)
        messages: list[ts.Msg] = []
        oversized = False
        if automated or (info.directory and not scope):
            pass  # decided from the listing: nothing to read
        else:
            try:
                directory, messages = ts.opencode_session(root, sid)
            except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError) as exc:
                if ts.is_oversized(exc):
                    oversized = True  # a property of the data: recorded with its key, re-read only when it changes
                else:
                    logger.warning("transcript_index: cannot read an opencode session: %s", type(exc).__name__)
                    _tick(stats, "opencode", "errors")
                    if isinstance(exc, sqlite3.Error) and ts.is_locked(exc):
                        return  # the producer is writing: try the rest next round
                    continue
            else:
                scope = scope or _scope_for_cwd(directory, run.scopes)  # the legacy layout reports it only here
                automated = ts.is_generated_cwd(directory) and not run.include_automated
        frags = _pack([] if automated else messages, scope, _mtime_iso(info.mtime_ms * 1_000_000))
        _write_session(conn, "opencode", rel, _opaque_id("opencode", sid), frags, replace=bool(row), size=info.size,
                       mtime_ns=info.mtime_ms, state=info.key, automated=automated)
        if automated:
            stats["files_automated"] += 1
        elif oversized:
            _tick(stats, "opencode", "skipped_oversized")
        elif not scope:
            _tick(stats, "opencode", "skipped_no_workspace")
        else:
            _tick(stats, "opencode", "files_indexed")
            stats["fragments_added"] += len(frags)


def _plan(root: Path | None) -> tuple[list[tuple[str, Path]], list[str]]:
    """``(sources to scan with their existing roots, sources switched off)``.  An explicit ``root`` is the Claude
    root alone: the other sources are neither scanned nor touched."""
    if root is not None:
        root = root.expanduser()
        return ([("claude", root)] if root.is_dir() else []), []
    roots = {name: source_root(name) for name in SOURCES}
    return ([(n, r) for n, r in roots.items() if r is not None and r.is_dir()],
            [n for n, r in roots.items() if r is None])


def index_transcripts(
    root: Path | None = None,
    index_path: Path | None = None,
    *,
    since_days: float | None = None,
    max_files: int | None = None,
    include_automated: bool = False,
) -> dict[str, int]:
    """Index new/changed transcripts (read-only scan) into the sidecar.

    ``root`` is the Claude projects root; given, only it is scanned.  Without it every source in
    :data:`SOURCES` is scanned at its default or ``MEMORYMASTER_TRANSCRIPT_ROOT[_<SOURCE>]`` location (a
    missing root is skipped; ``off`` switches a source off and drops what it had indexed).  One source failing
    never stops the others: it is counted in ``errors`` / ``<source>_errors``.  ``since_days`` skips sessions not
    modified inside that window and ``max_files`` stops each source after that many sessions (cheap caps for first
    runs on very large histories).  Returns counters.
    """
    index_path = index_path or default_index_path()
    plan, switched_off = _plan(root)
    stats = {"files_seen": 0, "files_indexed": 0, "files_skipped": 0, "fragments_added": 0, "files_automated": 0,
             "files_removed": 0, "rebuilt": 0, "errors": 0, "skipped_no_workspace": 0, "skipped_oversized": 0, "skipped_unreadable": 0,
             "sources_failed": 0}
    for name, _ in plan:
        stats.update({f"{name}_{key}": 0 for key in _PER_SOURCE})
    if not plan:
        return stats
    conn, rebuilt = _connect(index_path)
    stats["rebuilt"] = int(rebuilt)
    run = _Run(conn, stats, time.time() - since_days * 86400 if since_days else None, max_files,
               include_automated, {})
    with closing(conn):
        for name in switched_off:
            _drop_gone(conn, stats, name, set())
        for name, source_dir in plan:
            try:
                if name == "agy":
                    _index_agy(run, source_dir)
                elif name == "opencode":
                    _index_opencode(run, source_dir)
                else:
                    pattern = "*/*.jsonl" if name == "claude" else "*/*/*/rollout-*.jsonl"
                    _index_jsonl(run, name, source_dir, sorted(source_dir.glob(pattern)))
            except Exception as exc:  # noqa: BLE001 - one CLI's unreadable store never aborts the others
                logger.warning("transcript_index: source %s failed: %s", name, type(exc).__name__)
                _tick(stats, name, "errors")
                _failed(stats, name)
    return stats


MIN_ASKER_ID_CHARS = 8  # shorter ids are not session uuids: they would match unrelated rel suffixes


def _asker_sessions(conn: sqlite3.Connection, native: str) -> list[str]:
    """Index session ids of the asking session, from the id its CLI sends (``session_id`` of the hook payload).

    Index ids are opaque hashes, so the match runs on ``files.rel``, which keeps the native id in every source's
    own shape: Claude ``<project>/<id>.jsonl``, Codex ``codex:<y>/<m>/<d>/rollout-<stamp>-<id>.jsonl``, agy
    ``agy:<id>``, OpenCode ``opencode:<id>`` (the hook already sends it prefixed).  Nothing here depends on how the
    OS spells a path.  Known limit: a resumed/forked session is a NEW file with a new id that copies the old turns,
    so the older file is another session here and stays eligible.
    """
    if len(native) < MIN_ASKER_ID_CHARS:
        return []
    try:
        rows = conn.execute("SELECT rel, session_id FROM files WHERE instr(rel, ?) > 0", (native,)).fetchall()
    except sqlite3.Error:  # fail open: an asker that cannot be resolved excludes nothing
        return []
    ids = []
    for rel, sid in rows:
        if rel in (native, f"agy:{native}", f"{native}.jsonl") or rel.endswith((f"/{native}.jsonl", f"-{native}.jsonl")):
            ids.append(sid)
    return ids


def _fts_query(query: str) -> str:
    tokens = list(dict.fromkeys(_candidate_tokens(query)))[:MAX_QUERY_TOKENS]
    # Quote each token so FTS5 operators in user text (-, :, *) stay literal.
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def _rank(rows: list[sqlite3.Row], limit: int) -> list[sqlite3.Row]:
    """Relevance first; newest date first within one relevance band."""
    if not rows:
        return []
    best = max(-r["rank"] for r in rows) or 1.0
    # shortcut: fixed-width relevance bands, so two near-equal hits straddling a
    # band edge can order by relevance instead of date; upgrade if that matters.
    keyed = sorted(rows, key=lambda r: (-int((-r["rank"]) / best / RELEVANCE_BAND), _neg_date(r["date"])))
    return keyed[:limit]


def _neg_date(date: str) -> tuple:
    # ISO-8601 UTC strings sort chronologically; invert for "newest first".
    return tuple(-ord(c) for c in date)


def search(
    query: str,
    index_path: Path | None = None,
    *,
    limit: int = 5,
    scope: str | None = None,
    since: str | None = None,
    excerpt_chars: int = EXCERPT_CHARS,
    deadline_ms: float | None = None,
    asker: str | None = None,
    excluded: list[str] | None = None,
) -> list[TranscriptHit]:
    """Search the sidecar index; return redacted, dated, cited fragments.

    Read-only: opens the sidecar with ``mode=ro`` and returns ``[]`` when it
    does not exist yet.  ``scope`` matches exactly (``project:<slug>``) and is
    applied in SQL, before the top-k cut; ``since`` is an ISO date lower bound.
    The excerpt is the window of the fragment around the query terms (T-1862),
    cut from the already-redacted text so a window edge cannot split a secret.
    ``deadline_ms`` aborts the query once that wall-clock budget is spent
    (returns ``[]``), for callers on a latency budget such as the prompt hook.
    ``asker`` is the native session id of the session asking (see :func:`_asker_sessions`): its own fragments
    are left out in SQL, before the top-k cut, so the other sessions' fragments fill the window instead.  An
    asker that does not resolve excludes nothing; ``excluded``, when given, receives the index ids left out.
    """
    index_path = index_path or default_index_path()
    started = time.perf_counter()
    expires = started + deadline_ms / 1000.0 if deadline_ms is not None else None

    def spent() -> bool:
        return expires is not None and time.perf_counter() > expires

    match = _fts_query(query)
    if not match or not index_path.exists():
        return []
    sql = (
        "SELECT f.date, f.session_id, {source} AS source, f.scope, f.first_line, f.last_line, f.text, x.rank "
        "FROM fragments_fts x JOIN fragments f ON f.id = x.rowid WHERE fragments_fts MATCH ?"
    )
    args: list[object] = [match]
    if scope:
        sql += " AND f.scope = ?"
        args.append(scope)
    if since:
        sql += " AND f.date >= ?"
        args.append(since)
    tail = " ORDER BY x.rank LIMIT ?"
    limit_arg = max(limit * 10, 50)
    try:
        # the connect/busy wait is bounded by what is left of the deadline, not by the 2 s reader default
        wait = {} if expires is None else {"query_ms": max(1, int((expires - time.perf_counter()) * 1000))}
        with closing(connect_ro(index_path.resolve().as_posix(), **wait)) as conn:
            if spent():
                return []
            if conn.execute("PRAGMA user_version").fetchone()[0] != FORMAT_VERSION:
                logger.warning("transcript_index: index was built by an older format and is not served; "
                               "rebuild it with `memorymaster transcript-index`")
                return []
            # an index the next `transcript-index` has not migrated yet has no source column: all rows are Claude's
            sql = sql.format(source="f.source" if "source" in _columns(conn, "fragments") else "'claude'")
            if expires is not None:
                conn.set_progress_handler(lambda: 1 if time.perf_counter() > expires else 0, 5000)
            own = _asker_sessions(conn, asker) if isinstance(asker, str) else []
            if own:  # before the candidate LIMIT: filtering after it would leave nothing when the asker owns the window
                sql += f" AND f.session_id NOT IN ({','.join('?' * len(own))})"
                args.extend(own)
            if excluded is not None:
                excluded.extend(own)
            sql += tail
            args.append(limit_arg)
            rows = conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError as exc:
        # an expired deadline is the caller's budget working, not a fault
        (logger.debug if "interrupted" in str(exc) else logger.warning)("transcript_index: search failed: %s", exc)
        return []
    hits = []
    for row in _rank(rows, limit):
        if spent():  # ranking and redaction are inside the budget too
            return []
        redacted, _ = sanitize_persisted_text(row["text"])
        excerpt = centered_excerpt(redacted, query, excerpt_chars)
        hits.append(
            TranscriptHit(
                date=row["date"], session_id=row["session_id"], scope=row["scope"],
                first_line=row["first_line"], last_line=row["last_line"],
                score=round(-row["rank"], 4), excerpt=excerpt, source=row["source"],
            )
        )
    return hits
