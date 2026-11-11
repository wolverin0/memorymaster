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
* Sources (T-2277): Claude ``~/.claude/projects/*/*.jsonl``, Codex rollouts, agy exports and OpenCode sessions
  (``recall/transcript_sources.py``; each with an off switch), keyed by ``cli`` + session.  Read-only.  Only user/assistant
  ``text`` blocks are indexed; tool calls/results, sidechains and harness noise
  are skipped.
* Incremental: per-file ``(size, mtime_ns, byte offset, line count, prefix
  sha256)``.  Files are append-only, so a grown file is parsed from the stored
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
* Session id = ``sha256(<project-dir>/<file>)[:16]`` -- opaque (no username
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
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from memorymaster.core.scope_utils import scope_from_cwd
from memorymaster.core.security import sanitize_persisted_text
from memorymaster.recall import transcript_sources
from memorymaster.recall.recall_tokenizer import _candidate_tokens, centered_excerpt
from memorymaster.stores._storage_shared import connect_ro, open_conn

logger = logging.getLogger(__name__)

INDEX_ENV = "MEMORYMASTER_TRANSCRIPT_INDEX"
ROOT_ENV = "MEMORYMASTER_TRANSCRIPT_ROOT"

FORMAT_VERSION = 1  # PRAGMA user_version; 0 = built before sanitise-on-write (T-1876)
CHUNK_CHARS = 1800  # fragment size target (characters of "role: text")
MIN_MESSAGE_CHARS = 20
RELEVANCE_BAND = 0.05  # relevance fraction within which recency breaks ties
EXCERPT_CHARS = 600
MAX_QUERY_TOKENS = 12
# Headless `claude -p` / SDK runs (extraction, steward, benchmark harnesses) tag every
# record with this entrypoint; their transcripts quote prompts and answer keys.
AUTOMATED_ENTRYPOINT = transcript_sources.CLAUDE_AUTOMATED_ENTRYPOINT

_NOISE = re.compile(r"^(<local-command|<command-name>|<system-reminder>|\[MemoryMaster|Caveat:|<environment_context>"
                    r"|<user_instructions>|# AGENTS\.md instructions)", re.I)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    rel TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    offset INTEGER NOT NULL,
    lines INTEGER NOT NULL,
    prefix_sha TEXT NOT NULL,
    automated INTEGER NOT NULL DEFAULT 0,
    cli TEXT NOT NULL DEFAULT 'claude'
);
CREATE TABLE IF NOT EXISTS fragments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cli TEXT NOT NULL DEFAULT 'claude',
    session_id TEXT NOT NULL,
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
    cli: str = "claude"

    def to_dict(self) -> dict:
        return asdict(self)


def default_index_path() -> Path:
    raw = os.environ.get(INDEX_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".memorymaster" / "transcripts.db"


def default_root() -> Path:
    raw = os.environ.get(ROOT_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".claude" / "projects"


def _sid(rel: str) -> str:
    return hashlib.sha256(rel.encode()).hexdigest()[:16]


def session_id_for(root: Path, path: Path) -> str:
    return _sid(path.relative_to(root).as_posix())


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
    elif has_tables:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
        if "automated" not in columns:
            # T-2077 L28: additive, so a current-format index is not made unservable. Sessions that stored no
            # fragment are treated as skipped-automated once (a tool-only session just gets re-parsed, harmlessly).
            conn.execute("ALTER TABLE files ADD COLUMN automated INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE files SET automated = 1 WHERE session_id NOT IN (SELECT DISTINCT session_id FROM fragments)")
        if "cli" not in columns:  # T-2277: every row built before is a Claude row
            conn.execute("ALTER TABLE files ADD COLUMN cli TEXT NOT NULL DEFAULT 'claude'")
            conn.execute("ALTER TABLE fragments ADD COLUMN cli TEXT NOT NULL DEFAULT 'claude'")
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
    return role, _clean(content)


def _clean(content: str) -> str:
    """The message text worth indexing, sanitised whole (before segmentation and persistence, T-1876 N1); else ``""``."""
    text = content.strip()
    if len(text) < MIN_MESSAGE_CHARS or _NOISE.match(text):
        return ""
    return sanitize_persisted_text(text)[0]


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


class _Chunker:
    """Cuts messages into :data:`CHUNK_CHARS` fragments ``(scope, date, first_line, last_line, text)``; a fragment
    never spans two scopes.  Lines are JSONL line numbers (Claude/Codex) or message/step ordinals (agy/OpenCode)."""

    def __init__(self) -> None:
        self.fragments: list[tuple[str, str, int, int, str]] = []
        self.buf: list[str] = []
        self.size = self.first = self.last = 0
        self.date = self.scope = ""

    def flush(self) -> None:
        if self.buf:
            self.fragments.append((self.scope, self.date, self.first, self.last, "\n".join(self.buf)))
        self.buf, self.size = [], 0

    def add(self, scope: str, line: int, date: str, role: str, text: str) -> None:
        if self.buf and scope != self.scope:
            self.flush()
        for piece in _pieces(text):
            entry = f"{role}: {piece}"
            if self.buf and self.size + len(entry) > CHUNK_CHARS:
                self.flush()
            if not self.buf:
                self.first, self.date, self.scope = line, date, scope
            self.buf.append(entry)
            self.last = line
            self.size += len(entry)


def _parse_increment(path: Path, offset: int, line_no: int, fallback_date: str, skip_automated: bool,
                     hasher, scopes: dict[str, str], cli: str = "claude", cwd: str | None = None):
    """Parse complete lines from ``offset``; return ``(fragments, new_offset, new_lines, automated)``.

    ``hasher`` (the sha256 state of the bytes before ``offset``) is advanced over every consumed line.
    ``automated`` is True (and fragments empty) when ``skip_automated`` and a record
    carries the headless-session entrypoint; the caller records the file as seen.

    Claude scope comes from each record's own ``cwd``; a Codex rollout states its ``cwd`` once (``session_meta``),
    passed in as ``cwd``.  An unterminated trailing line is left for the next run.
    """
    chunker = _Chunker()
    state: dict = {}  # shortcut: Codex memory-tool linkage is per parse, not carried across appends; upgrade if echoes leak
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
            if skip_automated and transcript_sources.claude_automated(record):
                return [], offset, line_no, True
            if cli == "codex":
                role, text = transcript_sources.codex_message(record, state)
                text = _clean(text) if text else ""
                scope = _scope_for_cwd(cwd, scopes) if text else ""
            else:
                role, text = _message_text(record)
                scope = _scope_for_cwd(record.get("cwd"), scopes) if text else ""
            if text and scope:
                chunker.add(scope, this_line, _iso_date(record.get("timestamp"), fallback_date), role, text)
    chunker.flush()
    return chunker.fragments, offset, line_no, False


def _delete_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute(
        "INSERT INTO fragments_fts(fragments_fts, rowid, text) "
        "SELECT 'delete', id, text FROM fragments WHERE session_id = ?",
        (session_id,),
    )
    conn.execute("DELETE FROM fragments WHERE session_id = ?", (session_id,))


def _store(conn: sqlite3.Connection, rel: str, sid: str, cli: str, frags: list, state: tuple, *, replace: bool) -> None:
    """One file/session: its fragments (after dropping the old ones when ``replace``) and its ``files`` cursor row."""
    with conn:
        if replace:
            _delete_session(conn, sid)
        for scope, date, first, last, text in frags:
            cur = conn.execute(
                "INSERT INTO fragments(session_id, scope, date, first_line, last_line, text, cli) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", (sid, scope, date, first, last, text, cli))
            conn.execute("INSERT INTO fragments_fts(rowid, text) VALUES (?, ?)", (cur.lastrowid, text))
        conn.execute(
            "INSERT OR REPLACE INTO files(rel, session_id, size, mtime_ns, offset, lines, prefix_sha, automated, cli) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (rel, sid, *state, cli))


def _purge(conn: sqlite3.Connection, cli: str, keep: set[str], stats: dict[str, int]) -> None:
    """Drop the sessions of ``cli`` that are no longer on disk (``keep`` = rels still present)."""
    for gone in [r for r in conn.execute("SELECT rel, session_id FROM files WHERE cli = ?", (cli,)).fetchall()
                 if r["rel"] not in keep]:
        with conn:
            _delete_session(conn, gone["session_id"])
            conn.execute("DELETE FROM files WHERE rel = ?", (gone["rel"],))
        stats["files_removed"] += 1


def _index_whole(conn: sqlite3.Connection, cli: str, root: Path, stats: dict[str, int], scopes: dict[str, str],
                 cutoff: float | None, max_files: int | None) -> None:
    """agy/OpenCode: a session is re-read whole and replaced when its content digest changed."""
    try:
        sessions = transcript_sources.whole_sessions(cli, root)
    except (OSError, ValueError, sqlite3.Error) as exc:  # locked store / unreadable root: keep what is indexed
        logger.warning("transcript_index: %s source unavailable: %s", cli, type(exc).__name__)
        stats["errors"] += 1
        return
    _purge(conn, cli, {f"{cli}/{s.key}" for s in sessions}, stats)
    for sess in sessions:
        if max_files is not None and stats["files_indexed"] >= max_files:
            break
        if cutoff is not None and sess.stamp and sess.stamp[1] / 1e9 < cutoff:
            continue
        stats["files_seen"] += 1
        rel = f"{cli}/{sess.key}"
        sid = _sid(rel)
        row = conn.execute("SELECT * FROM files WHERE rel = ?", (rel,)).fetchone()
        if row and sess.stamp and (row["size"], row["mtime_ns"]) == sess.stamp:
            stats["files_skipped"] += 1
            continue
        try:
            cwd, messages = sess.load()
        except (OSError, ValueError, sqlite3.Error) as exc:  # AgySourceError is a ValueError
            logger.warning("transcript_index: cannot read %s session %s: %s", cli, sess.key, type(exc).__name__)
            stats["errors"] += 1
            continue
        digest = hashlib.sha256(json.dumps(messages).encode()).hexdigest()
        if row and row["prefix_sha"] == digest:
            stats["files_skipped"] += 1
            continue
        scope = _scope_for_cwd(cwd, scopes)
        chunker = _Chunker()
        for line, (stamp, role, raw_text) in enumerate(messages):
            text = _clean(raw_text) if scope else ""
            if text:
                chunker.add(scope, line, _iso_date(stamp, _mtime_iso(sess.stamp[1] if sess.stamp else 0)), role, text)
        chunker.flush()
        _store(conn, rel, sid, cli, chunker.fragments,
               (*(sess.stamp or (len(messages), 0)), 0, len(messages), digest, 0), replace=bool(row))
        stats["files_indexed"] += 1
        stats["fragments_added"] += len(chunker.fragments)


def _line_units(roots: dict[str, Path]):
    """``(cli, rel, path, stat)`` of every Claude/Codex transcript; ``rel`` is unprefixed for Claude (older rows)."""
    for cli in transcript_sources.LINE_CLIS:
        root = roots.get(cli)
        if root is None:
            continue
        for path in transcript_sources.line_files(cli, root) if root.is_dir() else []:
            rel = path.relative_to(root).as_posix()
            yield cli, rel if cli == "claude" else f"{cli}/{rel}", path


def index_transcripts(
    root: Path | None = None,
    index_path: Path | None = None,
    *,
    since_days: float | None = None,
    max_files: int | None = None,
    include_automated: bool = False,
    roots: dict[str, Path] | None = None,
) -> dict[str, int]:
    """Index new/changed transcripts (read-only scan) into the sidecar.

    ``root`` indexes that Claude root only (callers with a fixture or a custom root); ``roots`` maps CLI -> root;
    with neither, every enabled CLI is read from its default location (T-2277).  A source switched off with
    ``MEMORYMASTER_TRANSCRIPT_SOURCE_<CLI>=0`` is skipped and its rows are dropped.
    ``since_days`` skips files not modified inside that window (a cheap cap for
    first runs on very large histories).  Returns counters.
    """
    if roots is None:
        roots = {"claude": root} if root else {cli: (default_root() if cli == "claude" else transcript_sources.default_root(cli))
                                              for cli in transcript_sources.CLIS}
    roots = {cli: Path(path).expanduser() for cli, path in roots.items() if transcript_sources.enabled(cli)}
    index_path = index_path or default_index_path()
    stats = {"files_seen": 0, "files_indexed": 0, "files_skipped": 0, "fragments_added": 0, "files_automated": 0,
             "files_removed": 0, "rebuilt": 0, "errors": 0}
    cutoff = time.time() - since_days * 86400 if since_days else None
    scopes: dict[str, str] = {}
    if not index_path.is_file() and not any(path.is_dir() for path in roots.values()):
        return stats
    units = list(_line_units(roots))
    conn, rebuilt = _connect(index_path)
    stats["rebuilt"] = int(rebuilt)
    with closing(conn):
        for cli in transcript_sources.CLIS:  # a switched-off source stops being served AND stored
            if not transcript_sources.enabled(cli):
                _purge(conn, cli, set(), stats)
        # a transcript that no longer exists must stop being searchable (existence is judged over the whole
        # root, so --since-days / --max-files never read as a deletion)
        for cli in transcript_sources.LINE_CLIS:
            if cli in roots and roots[cli].is_dir():  # a root that is gone (unmounted) is not a mass deletion
                _purge(conn, cli, {rel for c, rel, _ in units if c == cli}, stats)
        for cli, rel, path in units:
            if max_files is not None and stats["files_indexed"] >= max_files:
                break
            try:
                st = path.stat()
            except OSError:
                stats["errors"] += 1
                continue
            if cutoff is not None and st.st_mtime < cutoff:
                continue
            stats["files_seen"] += 1
            sid = _sid(rel)
            row = conn.execute("SELECT * FROM files WHERE rel = ?", (rel,)).fetchone()
            # a session skipped as automated stored no fragments: --include-automated must re-parse it, and an
            # appended one is re-read from the start (its marker is in the head, not the tail)
            was_automated = bool(row and row["automated"])
            if (row and row["size"] == st.st_size and row["mtime_ns"] == st.st_mtime_ns
                    and not (was_automated and include_automated)):
                stats["files_skipped"] += 1
                continue
            offset, line_no = 0, 0
            try:
                resumable = row and st.st_size >= row["offset"] and not was_automated
                hasher = _verified_prefix(path, row["offset"], row["prefix_sha"]) if resumable else None
                if hasher is not None:
                    offset, line_no = row["offset"], row["lines"]
                else:
                    hasher = hashlib.sha256()  # new, shrunk or rewritten file: parse from the start
                cwd, headless = transcript_sources.codex_meta(path) if cli == "codex" else (None, False)
                if cli == "codex" and (cwd is None or (headless and not include_automated)):
                    # not attributable (no cwd / our own internal run) or a `codex exec` / sub-agent session
                    frags, new_offset, new_lines, automated = [], st.st_size, 0, True
                else:
                    frags, new_offset, new_lines, automated = _parse_increment(
                        path, offset, line_no, _mtime_iso(st.st_mtime_ns), not include_automated, hasher, scopes,
                        cli, cwd)
            except OSError as exc:
                logger.warning("transcript_index: cannot read %s: %s", rel, exc)
                stats["errors"] += 1
                continue
            _store(conn, rel, sid, cli, frags, (st.st_size, st.st_mtime_ns, new_offset, new_lines, hasher.hexdigest(),
                                                int(automated)), replace=bool(row and offset == 0))
            if automated:
                stats["files_automated"] += 1
                continue
            stats["files_indexed"] += 1
            stats["fragments_added"] += len(frags)
        for cli in ("agy", "opencode"):
            if cli in roots and roots[cli].is_dir():
                _index_whole(conn, cli, roots[cli], stats, scopes, cutoff, max_files)
    return stats


def coverage(index_path: Path | None = None, roots: dict[str, Path] | None = None) -> dict[str, dict]:
    """Per CLI: sessions ``present`` on disk, ``indexed`` (seen by the sidecar, automated ones included) and
    ``searchable`` (holding at least one fragment); ``roots`` defaults to the default roots."""
    index_path = index_path or default_index_path()
    roots = roots or {cli: (default_root() if cli == "claude" else transcript_sources.default_root(cli))
                      for cli in transcript_sources.CLIS}
    seen: dict[str, int] = {}
    found: dict[str, int] = {}
    if index_path.is_file():
        with closing(connect_ro(index_path.resolve().as_posix())) as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] == FORMAT_VERSION:
                seen = dict(conn.execute("SELECT cli, COUNT(*) FROM files GROUP BY cli").fetchall())
                found = dict(conn.execute("SELECT cli, COUNT(DISTINCT session_id) FROM fragments GROUP BY cli").fetchall())
    out = {}
    for cli, root in roots.items():
        present = (len(transcript_sources.line_files(cli, root)) if cli in transcript_sources.LINE_CLIS and root.is_dir()
                   else len(transcript_sources.whole_sessions(cli, root)) if cli not in transcript_sources.LINE_CLIS else 0)
        out[cli] = {"present": present, "indexed": seen.get(cli, 0), "searchable": found.get(cli, 0),
                    "coverage": round(min(seen.get(cli, 0), present) / present, 3) if present else None}
    return out


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
) -> list[TranscriptHit]:
    """Search the sidecar index; return redacted, dated, cited fragments.

    Read-only: opens the sidecar with ``mode=ro`` and returns ``[]`` when it
    does not exist yet.  ``scope`` matches exactly (``project:<slug>``) and is
    applied in SQL, before the top-k cut; ``since`` is an ISO date lower bound.
    The excerpt is the window of the fragment around the query terms (T-1862),
    cut from the already-redacted text so a window edge cannot split a secret.
    ``deadline_ms`` aborts the query once that wall-clock budget is spent
    (returns ``[]``), for callers on a latency budget such as the prompt hook.
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
        "SELECT f.date, f.session_id, f.scope, f.first_line, f.last_line, f.text, f.cli, x.rank "
        "FROM fragments_fts x JOIN fragments f ON f.id = x.rowid WHERE fragments_fts MATCH ?"
    )
    args: list[object] = [match]
    live = [cli for cli in transcript_sources.CLIS if transcript_sources.enabled(cli)]  # a switched-off source is not served
    sql += f" AND f.cli IN ({','.join('?' * len(live))})"
    args.extend(live)
    if scope:
        sql += " AND f.scope = ?"
        args.append(scope)
    if since:
        sql += " AND f.date >= ?"
        args.append(since)
    sql += " ORDER BY x.rank LIMIT ?"
    args.append(max(limit * 10, 50))
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
            if expires is not None:
                conn.set_progress_handler(lambda: 1 if time.perf_counter() > expires else 0, 5000)
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
                score=round(-row["rank"], 4), excerpt=excerpt, cli=row["cli"],
            )
        )
    return hits
