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
* Source: ``~/.claude/projects/*/*.jsonl`` (read-only).  Only user/assistant
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
AUTOMATED_ENTRYPOINT = "sdk-cli"

_NOISE = re.compile(r"^(<local-command|<command-name>|<system-reminder>|\[MemoryMaster|Caveat:)", re.I)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    rel TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    offset INTEGER NOT NULL,
    lines INTEGER NOT NULL,
    prefix_sha TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fragments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    def to_dict(self) -> dict:
        return asdict(self)


def default_index_path() -> Path:
    raw = os.environ.get(INDEX_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".memorymaster" / "transcripts.db"


def default_root() -> Path:
    raw = os.environ.get(ROOT_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".claude" / "projects"


def session_id_for(root: Path, path: Path) -> str:
    return hashlib.sha256(path.relative_to(root).as_posix().encode()).hexdigest()[:16]


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


def _parse_increment(path: Path, offset: int, line_no: int, fallback_date: str, skip_automated: bool,
                     hasher, scopes: dict[str, str]):
    """Parse complete lines from ``offset``; return ``(fragments, new_offset, new_lines, automated)``.

    ``hasher`` (the sha256 state of the bytes before ``offset``) is advanced over every consumed line.
    ``automated`` is True (and fragments empty) when ``skip_automated`` and a record
    carries the headless-session entrypoint; the caller records the file as seen.

    Fragments are ``(scope, date, first_line, last_line, text)``; scope comes from each record's own ``cwd``
    and a fragment never spans two scopes.  An unterminated trailing line that is not valid JSON is left for
    the next run.
    """
    fragments: list[tuple[str, str, int, int, str]] = []
    buf: list[str] = []
    size = 0
    first = last = 0
    date = fallback_date
    buf_scope = ""

    def flush() -> None:
        nonlocal buf, size
        if buf:
            fragments.append((buf_scope, date, first, last, "\n".join(buf)))
        buf, size = [], 0

    with path.open("rb") as fh:
        fh.seek(offset)
        for raw in fh:
            complete = raw.endswith(b"\n")
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                record = None
                if not complete:
                    break  # partial write: re-read next time
            offset += len(raw)
            hasher.update(raw)
            this_line = line_no
            line_no += 1
            if not isinstance(record, dict):
                continue
            if skip_automated and record.get("entrypoint") == AUTOMATED_ENTRYPOINT:
                return [], offset, line_no, True
            role, text = _message_text(record)
            scope = _scope_for_cwd(record.get("cwd"), scopes) if text else ""
            if not text or not scope:
                continue
            if buf and scope != buf_scope:
                flush()
            for piece in _pieces(text):
                entry = f"{role}: {piece}"
                if buf and size + len(entry) > CHUNK_CHARS:
                    flush()
                if not buf:
                    first = this_line
                    date = _iso_date(record.get("timestamp"), fallback_date)
                    buf_scope = scope
                buf.append(entry)
                last = this_line
                size += len(entry)
    flush()
    return fragments, offset, line_no, False


def _delete_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute(
        "INSERT INTO fragments_fts(fragments_fts, rowid, text) "
        "SELECT 'delete', id, text FROM fragments WHERE session_id = ?",
        (session_id,),
    )
    conn.execute("DELETE FROM fragments WHERE session_id = ?", (session_id,))


def index_transcripts(
    root: Path | None = None,
    index_path: Path | None = None,
    *,
    since_days: float | None = None,
    max_files: int | None = None,
    include_automated: bool = False,
) -> dict[str, int]:
    """Index new/changed transcripts under ``root`` (read-only scan) into the sidecar.

    ``since_days`` skips files not modified inside that window (a cheap cap for
    first runs on very large histories).  Returns counters.
    """
    root = (root or default_root()).expanduser()
    index_path = index_path or default_index_path()
    stats = {"files_seen": 0, "files_indexed": 0, "files_skipped": 0, "fragments_added": 0, "files_automated": 0,
             "files_removed": 0, "rebuilt": 0, "errors": 0}
    if not root.is_dir():
        return stats
    cutoff = time.time() - since_days * 86400 if since_days else None
    scopes: dict[str, str] = {}
    paths = sorted(root.glob("*/*.jsonl"))
    existing = {path.relative_to(root).as_posix() for path in paths}
    conn, rebuilt = _connect(index_path)
    stats["rebuilt"] = int(rebuilt)
    with closing(conn):
        # a transcript that no longer exists must stop being searchable (existence is judged over the whole
        # root, so --since-days / --max-files never read as a deletion)
        for gone in [r for r in conn.execute("SELECT rel, session_id FROM files").fetchall() if r["rel"] not in existing]:
            with conn:
                _delete_session(conn, gone["session_id"])
                conn.execute("DELETE FROM files WHERE rel = ?", (gone["rel"],))
            stats["files_removed"] += 1
        for path in paths:
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
            rel = path.relative_to(root).as_posix()
            sid = session_id_for(root, path)
            row = conn.execute("SELECT * FROM files WHERE rel = ?", (rel,)).fetchone()
            if row and row["size"] == st.st_size and row["mtime_ns"] == st.st_mtime_ns:
                stats["files_skipped"] += 1
                continue
            offset, line_no = 0, 0
            try:
                hasher = _verified_prefix(path, row["offset"], row["prefix_sha"]) if row and st.st_size >= row["offset"] else None
                if hasher is not None:
                    offset, line_no = row["offset"], row["lines"]
                else:
                    hasher = hashlib.sha256()  # new, shrunk or rewritten file: parse from the start
                frags, new_offset, new_lines, automated = _parse_increment(
                    path, offset, line_no, _mtime_iso(st.st_mtime_ns), not include_automated, hasher, scopes
                )
            except OSError as exc:
                logger.warning("transcript_index: cannot read %s: %s", rel, exc)
                stats["errors"] += 1
                continue
            with conn:
                if row and offset == 0:
                    _delete_session(conn, sid)  # file shrank/rewritten: rebuild
                for scope, date, first, last, text in frags:
                    cur = conn.execute(
                        "INSERT INTO fragments(session_id, scope, date, first_line, last_line, text) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (sid, scope, date, first, last, text),
                    )
                    conn.execute("INSERT INTO fragments_fts(rowid, text) VALUES (?, ?)", (cur.lastrowid, text))
                conn.execute(
                    "INSERT OR REPLACE INTO files(rel, session_id, size, mtime_ns, offset, lines, prefix_sha) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (rel, sid, st.st_size, st.st_mtime_ns, new_offset, new_lines, hasher.hexdigest()),
                )
            if automated:
                stats["files_automated"] += 1
                continue
            stats["files_indexed"] += 1
            stats["fragments_added"] += len(frags)
    return stats


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
        "SELECT f.date, f.session_id, f.scope, f.first_line, f.last_line, f.text, x.rank "
        "FROM fragments_fts x JOIN fragments f ON f.id = x.rowid WHERE fragments_fts MATCH ?"
    )
    args: list[object] = [match]
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
                score=round(-row["rank"], 4), excerpt=excerpt,
            )
        )
    return hits
