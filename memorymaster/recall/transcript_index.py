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
* Incremental: per-file ``(size, mtime_ns, byte offset, line count)``.  Files
  are append-only, so a grown file is parsed from the stored offset; an
  unchanged one is skipped; a shrunk one is re-indexed from scratch.
* Session id = ``sha256(<project-dir>/<file>)[:16]`` -- opaque (no username
  leaks from the encoded project directory) and identical to the T-1817 bench
  transcript id.  Fragments store the 0-based JSONL line range they cover.
* Ranking: FTS5 BM25 relevance first; among results whose relevance is within
  one band (``RELEVANCE_BAND``) of each other, newest date first.  Every hit
  carries its date so a stale conclusion is recognisable.
* Display text goes through ``core.security.sanitize_persisted_text``
  (secrets, encoded secrets, home paths) before it leaves this module.
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
from memorymaster.recall.recall_tokenizer import _candidate_tokens
from memorymaster.stores._storage_shared import connect_ro, open_conn

logger = logging.getLogger(__name__)

INDEX_ENV = "MEMORYMASTER_TRANSCRIPT_INDEX"
ROOT_ENV = "MEMORYMASTER_TRANSCRIPT_ROOT"

CHUNK_CHARS = 1800  # fragment size target (characters of "role: text")
MAX_MESSAGE_CHARS = 3 * CHUNK_CHARS  # longer messages keep their head only
MIN_MESSAGE_CHARS = 20
RELEVANCE_BAND = 0.05  # relevance fraction within which recency breaks ties
EXCERPT_CHARS = 600
MAX_QUERY_TOKENS = 12

_NOISE = re.compile(r"^(<local-command|<command-name>|<system-reminder>|\[MemoryMaster|Caveat:)", re.I)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    rel TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    offset INTEGER NOT NULL,
    lines INTEGER NOT NULL
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


def _connect(index_path: Path) -> sqlite3.Connection:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_conn(index_path)
    conn.executescript(_SCHEMA)
    return conn


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
    return role, text[:MAX_MESSAGE_CHARS]


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
    return [text[i:i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)]


def _parse_increment(path: Path, offset: int, line_no: int, fallback_date: str):
    """Parse complete lines from ``offset``; return ``(fragments, scope_cwd, new_offset, new_lines)``.

    Fragments are ``(date, first_line, last_line, text)``.  An unterminated
    trailing line that is not valid JSON is left for the next run.
    """
    fragments: list[tuple[str, int, int, str]] = []
    cwd = ""
    buf: list[str] = []
    size = 0
    first = last = 0
    date = fallback_date

    def flush() -> None:
        nonlocal buf, size
        if buf:
            fragments.append((date, first, last, "\n".join(buf)))
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
            this_line = line_no
            line_no += 1
            if not isinstance(record, dict):
                continue
            if not cwd and isinstance(record.get("cwd"), str):
                cwd = record["cwd"]
            role, text = _message_text(record)
            if not text:
                continue
            for piece in _pieces(text):
                entry = f"{role}: {piece}"
                if buf and size + len(entry) > CHUNK_CHARS:
                    flush()
                if not buf:
                    first = this_line
                    date = _iso_date(record.get("timestamp"), fallback_date)
                buf.append(entry)
                last = this_line
                size += len(entry)
    flush()
    return fragments, cwd, offset, line_no


def _scope_for_cwd(cwd: str) -> str:
    try:
        return scope_from_cwd(cwd) if cwd else "project:unknown"
    except (OSError, ValueError, RuntimeError):
        return "project:unknown"


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
) -> dict[str, int]:
    """Index new/changed transcripts under ``root`` (read-only scan) into the sidecar.

    ``since_days`` skips files not modified inside that window (a cheap cap for
    first runs on very large histories).  Returns counters.
    """
    root = (root or default_root()).expanduser()
    index_path = index_path or default_index_path()
    stats = {"files_seen": 0, "files_indexed": 0, "files_skipped": 0, "fragments_added": 0, "errors": 0}
    if not root.is_dir():
        return stats
    cutoff = time.time() - since_days * 86400 if since_days else None
    scope_cache: dict[str, str] = {}
    with closing(_connect(index_path)) as conn:
        for path in sorted(root.glob("*/*.jsonl")):
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
            offset, line_no, scope = 0, 0, ""
            if row and st.st_size >= row["offset"]:
                offset, line_no, scope = row["offset"], row["lines"], row["scope"]
            try:
                frags, cwd, new_offset, new_lines = _parse_increment(
                    path, offset, line_no, _mtime_iso(st.st_mtime_ns)
                )
            except OSError as exc:
                logger.warning("transcript_index: cannot read %s: %s", rel, exc)
                stats["errors"] += 1
                continue
            if not scope:
                scope = scope_cache.get(cwd) or scope_cache.setdefault(cwd, _scope_for_cwd(cwd))
            with conn:
                if row and offset == 0:
                    _delete_session(conn, sid)  # file shrank/rewritten: rebuild
                for date, first, last, text in frags:
                    cur = conn.execute(
                        "INSERT INTO fragments(session_id, scope, date, first_line, last_line, text) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (sid, scope, date, first, last, text),
                    )
                    conn.execute("INSERT INTO fragments_fts(rowid, text) VALUES (?, ?)", (cur.lastrowid, text))
                conn.execute(
                    "INSERT OR REPLACE INTO files(rel, session_id, scope, size, mtime_ns, offset, lines) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (rel, sid, scope, st.st_size, st.st_mtime_ns, new_offset, new_lines),
                )
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
) -> list[TranscriptHit]:
    """Search the sidecar index; return redacted, dated, cited fragments.

    Read-only: opens the sidecar with ``mode=ro`` and returns ``[]`` when it
    does not exist yet.  ``scope`` matches exactly (``project:<slug>``);
    ``since`` is an ISO date lower bound.
    """
    index_path = index_path or default_index_path()
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
        with closing(connect_ro(index_path.resolve().as_posix())) as conn:
            rows = conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError as exc:
        logger.warning("transcript_index: search failed: %s", exc)
        return []
    hits = []
    for row in _rank(rows, limit):
        excerpt, _ = sanitize_persisted_text(row["text"][:EXCERPT_CHARS])
        hits.append(
            TranscriptHit(
                date=row["date"], session_id=row["session_id"], scope=row["scope"],
                first_line=row["first_line"], last_line=row["last_line"],
                score=round(-row["rank"], 4), excerpt=excerpt,
            )
        )
    return hits
