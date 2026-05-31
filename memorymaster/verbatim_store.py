"""Verbatim memory store — raw conversation storage with vector search.

Stores full conversation text without summarization or extraction.
Complements the claims DB: claims = curated knowledge, verbatim = raw recall.

Search modes:
  - FTS5 for keyword search (fast, local)
  - Qdrant for semantic search (when available)
  - Hybrid: FTS5 + Qdrant merged results
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
from typing import Any

# Credential detection delegated to the canonical filter in memorymaster.security.
from memorymaster.security import redact_text as _redact_text

logger = logging.getLogger(__name__)



def _contains_sensitive(text: str) -> bool:
    _, findings = _redact_text(text)
    return bool(findings)


def _row_has_sensitive_field(role: str, source_agent: str, content: str) -> bool:
    """Defense-in-depth: check role and source_agent in addition to content.

    F-4 fix (overnight audit 2026-05-04): role and source_agent are
    user-controlled in some upstream paths (CLI flags, dream-bridge config,
    transcript miner). A maliciously crafted or misconfigured
    source_agent='Bearer ghp_xxx...' would persist a token to
    verbatim_memories undetected if we only checked content. The canonical
    redact_text covers all three fields here — refuse the whole row if any
    finding appears anywhere. Don't redact-and-store; just drop.
    """
    joined = " | ".join(filter(None, (role, source_agent, content)))
    _, findings = _redact_text(joined)
    return bool(findings)

# Vector search is opt-in: an unset QDRANT_URL means "vector disabled", exactly
# like a missing OPENAI_API_KEY. NEVER hardcode a routable private LAN IP here —
# a home-lab RFC1918 default previously shipped to PyPI, violating the
# "never hardcode IPs" boundary and silently pointing installs at the author's
# network. Mirror qdrant_backend.py / service.py which use empty/localhost defaults.
QDRANT_URL = os.environ.get("QDRANT_URL", "").strip()
QDRANT_COLLECTION = "memorymaster-verbatim"
EMBED_DIM = 1536  # text-embedding-3-small


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


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
            timestamp,
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
    if _row_has_sensitive_field(role or "", source_agent or "", content):
        return None

    now = timestamp or datetime.now(timezone.utc).isoformat()

    # Dedup by exact content within the same session. Uses idx_verbatim_session
    # so the lookup is O(rows-in-session), not table-scan. The previous FTS5-based
    # dedup query passed a sha256 hex prefix to MATCH which never resolves -
    # the FTS5 index stores the content text, not its hash. Result: 9M+ rows
    # accumulated for orchestrator sessions because every Stop event re-inserted
    # every message. Fixed 2026-05-03; see mm-0c43.
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
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue

            role, content = _extract_role_content(entry)
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

    mode: "fts" (keyword), "vector" (Qdrant), "hybrid" (both merged)
    """
    results = []

    if mode in ("fts", "hybrid"):
        results.extend(_search_fts(db_path, query, scope, limit))

    if mode in ("vector", "hybrid"):
        vector_results = _search_vector(query, scope, limit)
        # Merge: dedupe by stable row-id key (see _row_dedup_key for F-3 context)
        seen = {_row_dedup_key(r) for r in results}
        for vr in vector_results:
            key = _row_dedup_key(vr)
            if key not in seen:
                results.append(vr)
                seen.add(key)

    # Sort by score descending, limit
    results.sort(key=lambda x: -x.get("score", 0))
    return results[:limit]


def _search_fts(db_path: str, query: str, scope: str | None, limit: int) -> list[dict]:
    """FTS5 keyword search over verbatim memories."""
    # Clean query for FTS5
    clean_query = " ".join(w for w in query.split() if len(w) > 2)
    if not clean_query:
        return []

    # closing() guarantees the connection is released even if the JOIN raises a
    # non-OperationalError (corrupt/locked DB, programming error) — the bare
    # except below only catches OperationalError.
    with closing(_connect(db_path)) as conn:
        try:
            if scope:
                rows = conn.execute(
                    """SELECT v.id, v.session_id, v.role, v.content, v.scope, v.timestamp,
                              rank as score
                       FROM verbatim_fts f
                       JOIN verbatim_memories v ON v.id = f.rowid
                       WHERE verbatim_fts MATCH ? AND v.scope LIKE ?
                       ORDER BY rank
                       LIMIT ?""",
                    (clean_query, f"{scope}%", limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT v.id, v.session_id, v.role, v.content, v.scope, v.timestamp,
                              rank as score
                       FROM verbatim_fts f
                       JOIN verbatim_memories v ON v.id = f.rowid
                       WHERE verbatim_fts MATCH ?
                       ORDER BY rank
                       LIMIT ?""",
                    (clean_query, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            rows = []

    return [
        {"id": r["id"], "session_id": r["session_id"], "role": r["role"],
         "content": r["content"], "scope": r["scope"], "timestamp": r["timestamp"],
         "score": abs(r["score"]) if r["score"] else 0, "source": "fts"}
        for r in rows
    ]


def _search_vector(query: str, scope: str | None, limit: int) -> list[dict]:
    """Qdrant semantic search over verbatim memories."""
    if not QDRANT_URL:
        return []
    try:
        # Embed query with OpenAI
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            return []

        embed_url = "https://api.openai.com/v1/embeddings"
        payload = {"model": "text-embedding-3-small", "input": [query]}
        req = urllib.request.Request(
            embed_url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
        vector = result["data"][0]["embedding"]

        # Search Qdrant
        search_payload: dict[str, Any] = {
            "vector": vector,
            "limit": limit,
            "with_payload": True,
        }
        if scope:
            search_payload["filter"] = {"must": [{"key": "scope", "match": {"value": scope}}]}

        req = urllib.request.Request(
            f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/search",
            data=json.dumps(search_payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())

        return [
            {"id": h.get("id"), "content": h["payload"].get("content", ""),
             "content_hash": h["payload"].get("content_hash", ""),
             "scope": h["payload"].get("scope", ""),
             "session_id": h["payload"].get("session_id", ""), "role": h["payload"].get("role", ""),
             "score": h.get("score", 0), "source": "vector"}
            for h in result.get("result", [])
        ]
    except Exception:
        return []


def sync_to_qdrant(db_path: str, batch_size: int = 50) -> dict[str, int]:
    """Sync unsynced verbatim memories to Qdrant for vector search."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return {"synced": 0, "error": "no OPENAI_API_KEY"}
    if not QDRANT_URL:
        return {"synced": 0, "error": "no QDRANT_URL"}

    # closing() guarantees the connection is released on every exit path,
    # including the initial SELECT raising or the final UPDATE/commit raising.
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT id, content, scope, session_id, role FROM verbatim_memories WHERE embedding_synced = 0 LIMIT ?",
            (batch_size,),
        ).fetchall()

        if not rows:
            return {"synced": 0}

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
        texts = [r["content"][:2000] for r in rows]
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

        # Upsert to Qdrant
        points = []
        for i, (row, emb) in enumerate(zip(rows, embeddings)):
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
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(f"UPDATE verbatim_memories SET embedding_synced = 1 WHERE id IN ({placeholders})", ids)
        conn.commit()

        return {"synced": len(rows)}
