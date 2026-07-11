"""Verbatim memory store — raw conversation storage with authoritative search.

Stores full conversation text without summarization or extraction.
Complements the claims DB: claims = curated knowledge, verbatim = raw recall.

Search modes:
  - FTS5 for keyword search (fast, local)
  - Vector/hybrid requests currently downgrade to FTS5
  - Qdrant remains available only for index synchronization during quarantine
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import urllib.request
import urllib.error
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

# Credential detection delegated to the canonical filter in memorymaster.core.security.
from memorymaster.core import spool
from memorymaster.core.security import scan_persisted_value
from memorymaster.stores._storage_shared import open_conn

logger = logging.getLogger(__name__)



def _contains_sensitive(text: str) -> bool:
    return bool(scan_persisted_value(text))


def _row_has_sensitive_field(
    role: str,
    source_agent: str,
    content: str,
    *,
    session_id: str = "",
    scope: str = "",
    timestamp: str = "",
    created_at: str = "",
) -> bool:
    """Defense-in-depth: scan every textual field persisted for a turn.

    F-4 fix (overnight audit 2026-05-04): role and source_agent are
    user-controlled in some upstream paths (CLI flags, dream-bridge config,
    transcript miner). A maliciously crafted or misconfigured
    source_agent='Bearer ghp_xxx...' would persist a token to
    verbatim_memories undetected if we only checked content. The decoded
    durable-envelope scanner covers current and encoded secret
    shapes across all fields. Don't redact-and-store verbatim content; drop
    the complete row so callers never mistake a marker for a raw transcript.
    """
    return bool(
        scan_persisted_value(
            {
                "session_id": session_id,
                "role": role,
                "content": content,
                "scope": scope,
                "timestamp": timestamp,
                "source_agent": source_agent,
                "created_at": created_at,
            }
        )
    )


def _row_value(row: sqlite3.Row | dict, field: str, default: object = "") -> object:
    """Read an optional row field across current and legacy schemas."""
    try:
        return row[field] if field in row.keys() else default
    except (AttributeError, KeyError, TypeError, IndexError):
        return default


def _verbatim_row_has_sensitive_field(row: sqlite3.Row | dict) -> bool:
    """Apply the durable envelope scanner to a current or legacy DB row."""
    return _row_has_sensitive_field(
        str(_row_value(row, "role") or ""),
        str(_row_value(row, "source_agent") or ""),
        str(_row_value(row, "content") or ""),
        session_id=str(_row_value(row, "session_id") or ""),
        scope=str(_row_value(row, "scope") or ""),
        timestamp=str(_row_value(row, "timestamp") or ""),
        created_at=str(_row_value(row, "created_at") or ""),
    )

# Vector search is opt-in: an unset QDRANT_URL means "vector disabled", exactly
# like a missing OPENAI_API_KEY. NEVER hardcode a routable private LAN IP here —
# a home-lab RFC1918 default previously shipped to PyPI, violating the
# "never hardcode IPs" boundary and silently pointing installs at the author's
# network. Mirror qdrant_backend.py / service.py which use empty/localhost defaults.
QDRANT_URL = os.environ.get("QDRANT_URL", "").strip()
QDRANT_COLLECTION = "memorymaster-verbatim"
EMBED_DIM = 1536  # text-embedding-3-small


def _connect(db_path: str) -> sqlite3.Connection:
    # open_conn supplies WAL + busy_timeout=15000. The timeout is mandatory
    # on this connection: verbatim_memories is the hottest write path (Stop
    # hook + MCP per-turn inserts), and the 2026-06-05 btree corruption was
    # confined to idx_verbatim_session on this exact table. Without it, the
    # loser of a write race raises "database is locked" immediately and
    # drops the turn (spec §2.1/§2.8).
    return open_conn(db_path)


# Mirrors the live DB's DDL exactly (verified via sqlite_master 2026-06-10).
# Historically the table was created out-of-band by the Stop hook; under the
# P1 spool regime (spec §2.3) the hook never opens the DB, so the spool
# drainer needs a first-class way to create it on a fresh DB.
_VERBATIM_SCHEMA = """
CREATE TABLE IF NOT EXISTS verbatim_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'project',
    timestamp TEXT NOT NULL,
    source_agent TEXT DEFAULT '',
    embedding_synced INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS verbatim_fts USING fts5(
    content,
    content='verbatim_memories',
    content_rowid='id',
    tokenize='porter unicode61'
);
CREATE INDEX IF NOT EXISTS idx_verbatim_session ON verbatim_memories(session_id);
CREATE INDEX IF NOT EXISTS idx_verbatim_session_content
    ON verbatim_memories(session_id, content);
"""


def ensure_verbatim_schema(db_path: str) -> None:
    """Create the verbatim tables/indexes if absent (idempotent)."""
    with closing(_connect(db_path)) as conn:
        conn.executescript(_VERBATIM_SCHEMA)
        conn.commit()


def store_verbatim(
    db_path: str,
    session_id: str,
    role: str,
    content: str,
    scope: str = "project",
    source_agent: str = "",
    timestamp: str | None = None,
) -> int | None:
    """Store a verbatim conversation turn. Returns row ID or None if filtered."""
    now = timestamp or datetime.now(timezone.utc).isoformat()
    if not content or len(content) < 20:
        return None
    if _row_has_sensitive_field(
        role or "",
        source_agent or "",
        content,
        session_id=session_id or "",
        scope=scope or "",
        timestamp=now,
    ):
        return None

    # closing() guarantees the connection (and its WAL write lock) is released
    # even if an INSERT/commit raises (e.g. "database is locked" under
    # concurrent MCP/Stop-hook writers) - this is the hottest write path.
    with closing(_connect(db_path)) as conn:
        row_id = _store_verbatim_conn(
            conn,
            session_id,
            role,
            content,
            scope,
            source_agent,
            now,
        )
        conn.commit()
        return row_id


def _store_verbatim_conn(
    conn: sqlite3.Connection,
    session_id: str,
    role: str,
    content: str,
    scope: str = "project",
    source_agent: str = "",
    timestamp: str | None = None,
) -> int | None:
    """Store one turn using an existing connection without committing."""
    if not content or len(content) < 20:
        return None

    now = timestamp or datetime.now(timezone.utc).isoformat()
    if _row_has_sensitive_field(
        role or "",
        source_agent or "",
        content,
        session_id=session_id or "",
        scope=scope or "",
        timestamp=now,
    ):
        return None

    # Dedup by exact content within the same session. The composite
    # idx_verbatim_session_content(session_id, content) (migration 0006) makes
    # this an index seek/probe instead of an O(rows-in-session) scan that
    # byte-compares the ~262 KB content of every other row in the session — the
    # equality predicate is sargable against the leading session_id + content
    # columns. Without that index SQLite can only use the single-column
    # idx_verbatim_session and re-reads every content blob, which on hot
    # orchestrator sessions dominated insert time.
    #
    # The previous FTS5-based dedup query passed a sha256 hex prefix to MATCH
    # which never resolves - the FTS5 index stores the content text, not its
    # hash. Result: 9M+ rows accumulated for orchestrator sessions because every
    # Stop event re-inserted every message. Fixed 2026-05-03; see mm-0c43.
    existing = conn.execute(
        "SELECT id FROM verbatim_memories WHERE session_id = ? AND content = ? LIMIT 1",
        (session_id, content),
    ).fetchone()
    if existing:
        return None

    cur = conn.execute(
        """INSERT INTO verbatim_memories (session_id, role, content, scope, timestamp, source_agent)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, role, content, scope, now, source_agent),
    )
    row_id = cur.lastrowid

    # Update FTS
    conn.execute(
        "INSERT INTO verbatim_fts(rowid, content) VALUES (?, ?)",
        (row_id, content),
    )
    return row_id


def _extract_role_content(entry: dict) -> tuple[str, str]:
    """Pull ``(role, content)`` from one transcript line.

    Claude Code transcripts nest the turn under ``message``
    (``{"type":"user","message":{"role":..,"content":..}}``); older/simple
    transcripts put role+content at the top level. Prefer the nested shape and
    fall back to top-level. ``content`` may be a string or a list of content
    blocks — only ``text`` blocks are kept (tool_use/tool_result are dropped).

    Bug history (2026-05-21): this previously read only the top-level fields,
    so against real Claude Code transcripts ``role`` was always empty and the
    only rows stored were the handful of non-conversation metadata lines that
    happen to carry a top-level ``content`` (custom titles, summaries, internal
    prompts). 744k rows accumulated with zero real turns and zero roles.
    """
    msg = entry.get("message")
    if isinstance(msg, dict):
        role = msg.get("role", "") or ""
        content = msg.get("content", "")
    else:
        role = entry.get("role", "") or ""
        content = entry.get("content", "")
    if isinstance(content, list):
        content = " ".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return role, content if isinstance(content, str) else ""


def _iter_transcript_turns(path: Path):
    """Yield ``(role, content)`` for every parseable dict line of a transcript.

    Shared by :func:`store_transcript` (direct DB path) and
    :func:`spool_transcript` (P1 spool path) so the line parsing can never
    drift between the two regimes — a turn one path keeps and the other
    drops would silently change what verbatim recall can find when the
    operator flips MEMORYMASTER_WAL_DISCIPLINE.
    """
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        yield _extract_role_content(entry)


def store_transcript(
    db_path: str,
    transcript_path: str | Path,
    scope: str = "project",
    source_agent: str = "transcript",
) -> dict[str, int]:
    """Ingest an entire JSONL transcript into verbatim storage.

    Stores only user/assistant text turns; metadata lines (titles, snapshots,
    summaries) and tool-only turns are skipped.
    """
    path = Path(transcript_path)
    if not path.exists():
        return {"stored": 0, "skipped": 0, "error": "file not found"}

    stats = {"stored": 0, "skipped": 0}
    session_id = path.stem  # Use filename as session ID

    with closing(_connect(db_path)) as conn:
        for role, content in _iter_transcript_turns(path):
            if role not in ("user", "assistant"):
                stats["skipped"] += 1
                continue
            if not content or len(content) < 20:
                stats["skipped"] += 1
                continue

            row_id = _store_verbatim_conn(conn, session_id, role, content, scope, source_agent)
            if row_id:
                stats["stored"] += 1
            else:
                stats["skipped"] += 1
        conn.commit()

    return stats


def spool_transcript(
    db_path: str,
    transcript_path: str | Path,
    scope: str = "project",
    source_agent: str = "transcript",
) -> dict[str, int]:
    """Spool a JSONL transcript as ``op:"verbatim"`` envelopes (spec §2.3).

    Flag-on counterpart of :func:`store_transcript`: the Stop hook fires on
    every stop, and under MEMORYMASTER_WAL_DISCIPLINE it must NOT open the
    multi-GB DB — each kept turn becomes a ~10 ms spool append and the
    steward drain replays it through :func:`store_verbatim` (its per-session
    dedup and sensitivity filter intact). The sensitivity check ALSO runs
    here, before the append, so a credential never sits at rest in the
    plaintext spool file waiting for the drain to reject it.
    """
    path = Path(transcript_path)
    if not path.exists():
        return {"spooled": 0, "skipped": 0, "error": "file not found"}

    stats = {"spooled": 0, "skipped": 0}
    session_id = path.stem  # Use filename as session ID — mirrors store_transcript
    now = datetime.now(timezone.utc).isoformat()

    for role, content in _iter_transcript_turns(path):
        if role not in ("user", "assistant"):
            stats["skipped"] += 1
            continue
        if not content or len(content) < 20:
            stats["skipped"] += 1
            continue
        if _row_has_sensitive_field(role, source_agent or "", content):
            stats["skipped"] += 1
            continue
        spool.append(
            db_path,
            "verbatim",
            {
                "session_id": session_id,
                "role": role,
                "content": content,
                "scope": scope,
                "source_agent": source_agent,
                "timestamp": now,
            },
        )
        stats["spooled"] += 1

    return stats


def _row_dedup_key(r: dict) -> tuple:
    """Stable unique key for hybrid-mode search-result dedup.

    F-3 fix (overnight audit 2026-05-04): the previous merge keyed on
    `content[:100]`, which collides massively on templated content. Verified
    against the live DB: 4258 distinct 100-char prefixes collide; the worst
    offender (orchestrator <task-notification> templates) had 25,894 rows
    sharing one prefix. Hybrid mode silently collapsed all 25,894 distinct
    messages into a single returned row.

    Prefer the row id (always unique). Fall back to (session_id, content hash)
    tuple when id is missing — happens for vector results from Qdrant which
    didn't pull point.id into the payload dict.
    """
    rid = r.get("id")
    if rid is not None:
        return ("id", rid)
    content = r.get("content", "")
    # use full-content hash instead of 100-char prefix to avoid template-collision
    content_hash = r.get("content_hash") or hashlib.sha256(content.encode()).hexdigest()
    return ("sch", r.get("session_id", ""), content_hash)


def search_verbatim(
    db_path: str,
    query: str,
    scope: str | None = None,
    limit: int = 10,
    mode: str = "fts",
) -> list[dict]:
    """Search verbatim memories.

    Qdrant-backed ``vector`` and ``hybrid`` requests are temporarily
    downgraded to authoritative FTS until governed rehydration is available.
    """
    requested_mode = str(mode).strip().lower()
    effective_mode = "fts" if requested_mode in {"vector", "hybrid"} else requested_mode
    if effective_mode != "fts":
        return []

    results = _search_fts(db_path, query, scope, limit)

    # Sort by score descending, limit
    results.sort(key=lambda x: -x.get("score", 0))
    return results[:limit]


def _search_fts(db_path: str, query: str, scope: str | None, limit: int) -> list[dict]:
    """FTS5 keyword search over verbatim memories."""
    # Clean query for FTS5
    clean_query = " ".join(w for w in query.split() if len(w) > 2)
    if not clean_query:
        return []
    fetch_limit = max(limit * 5, limit + 20)

    # closing() guarantees the connection is released even if the JOIN raises a
    # non-OperationalError (corrupt/locked DB, programming error) — the bare
    # except below only catches OperationalError.
    with closing(_connect(db_path)) as conn:
        try:
            if scope:
                rows = conn.execute(
                    """SELECT v.*, rank as score
                       FROM verbatim_fts f
                       JOIN verbatim_memories v ON v.id = f.rowid
                       WHERE verbatim_fts MATCH ?
                         AND (v.scope = ? OR (? = 'project' AND v.scope LIKE 'project:%'))
                       ORDER BY rank
                       LIMIT ?""",
                    (clean_query, scope, scope, fetch_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT v.*, rank as score
                       FROM verbatim_fts f
                       JOIN verbatim_memories v ON v.id = f.rowid
                       WHERE verbatim_fts MATCH ?
                       ORDER BY rank
                       LIMIT ?""",
                    (clean_query, fetch_limit),
                ).fetchall()
        except sqlite3.OperationalError:
            rows = []

    results: list[dict] = []
    for row in rows:
        if _verbatim_row_has_sensitive_field(row):
            continue
        results.append(
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "role": row["role"],
                "content": row["content"],
                "scope": row["scope"],
                "timestamp": row["timestamp"],
                "score": abs(row["score"]) if row["score"] else 0,
                "source": "fts",
            }
        )
        if len(results) >= limit:
            break
    return results


def _search_vector(query: str, scope: str | None, limit: int) -> list[dict]:
    """Reject raw Qdrant payload reads until governed rehydration exists."""
    del query, scope, limit
    raise PermissionError(
        "Verbatim Qdrant retrieval is quarantined pending authoritative rehydration."
    )


def sync_to_qdrant(db_path: str, batch_size: int = 50) -> dict[str, int]:
    """Sync verbatim rows to the Qdrant index; read retrieval is quarantined."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return {"synced": 0, "error": "no OPENAI_API_KEY"}
    if not QDRANT_URL:
        return {"synced": 0, "error": "no QDRANT_URL"}

    # closing() guarantees the connection is released on every exit path,
    # including the initial SELECT raising or the final UPDATE/commit raising.
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM verbatim_memories WHERE embedding_synced = 0 LIMIT ?",
            (batch_size,),
        ).fetchall()

        if not rows:
            return {"synced": 0}

        safe_rows: list[sqlite3.Row] = []
        unsafe_ids: list[int] = []
        for row in rows:
            if _verbatim_row_has_sensitive_field(row):
                unsafe_ids.append(int(row["id"]))
            else:
                safe_rows.append(row)
        excluded_sensitive = len(unsafe_ids)
        if unsafe_ids:
            placeholders = ",".join("?" for _ in unsafe_ids)
            conn.execute(
                f"UPDATE verbatim_memories SET embedding_synced = -1 WHERE id IN ({placeholders})",
                unsafe_ids,
            )
            conn.commit()
        if not safe_rows:
            return {"synced": 0, "excluded_sensitive": excluded_sensitive}

        # Ensure collection exists
        try:
            req = urllib.request.Request(f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}")
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            payload = {"vectors": {"size": EMBED_DIM, "distance": "Cosine"}}
            req = urllib.request.Request(
                f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="PUT",
            )
            try:
                urllib.request.urlopen(req, timeout=60)
            except Exception as e:
                return {"synced": 0, "error": str(e)}

        # Embed in batches
        texts = [r["content"][:2000] for r in safe_rows]
        try:
            embed_url = "https://api.openai.com/v1/embeddings"
            payload = {"model": "text-embedding-3-small", "input": texts}
            req = urllib.request.Request(
                embed_url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
            embeddings = [d["embedding"] for d in result["data"]]
        except Exception as e:
            return {"synced": 0, "error": str(e)}
        if len(embeddings) != len(safe_rows):
            return {"synced": 0, "error": "embedding response cardinality mismatch"}

        # Upsert to Qdrant
        points = []
        for row, emb in zip(safe_rows, embeddings):
            points.append({
                "id": row["id"],
                "vector": emb,
                "payload": {
                    "content": row["content"][:2000],
                    "content_hash": hashlib.sha256(row["content"].encode()).hexdigest(),
                    "scope": row["scope"],
                    "session_id": row["session_id"],
                    "role": row["role"],
                },
            })

        try:
            req = urllib.request.Request(
                f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points",
                data=json.dumps({"points": points}).encode(),
                headers={"Content-Type": "application/json"},
                method="PUT",
            )
            urllib.request.urlopen(req, timeout=30)
        except Exception as e:
            return {"synced": 0, "error": str(e)}

        # Mark as synced
        ids = [r["id"] for r in safe_rows]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(f"UPDATE verbatim_memories SET embedding_synced = 1 WHERE id IN ({placeholders})", ids)
        conn.commit()

        result = {"synced": len(safe_rows)}
        if excluded_sensitive:
            result["excluded_sensitive"] = excluded_sensitive
        return result
