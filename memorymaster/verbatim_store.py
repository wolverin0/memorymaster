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
import re
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Credential detection delegated to the canonical filter in
# memorymaster.security — single source of truth.
from memorymaster.security import redact_text as _redact_text


def _contains_sensitive(text: str) -> bool:
    _, findings = _redact_text(text)
    return bool(findings)

QDRANT_URL = os.environ.get("QDRANT_URL", "http://192.168.100.186:6333")
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
    if not content or len(content) < 20:
        return None
    if _contains_sensitive(content):
        return None

    now = timestamp or datetime.now(timezone.utc).isoformat()
    content_hash = hashlib.sha256(content[:500].lower().encode()).hexdigest()[:16]

    conn = _connect(db_path)
    # Dedup by content hash within same session
    existing = conn.execute(
        "SELECT id FROM verbatim_memories WHERE session_id = ? AND id IN "
        "(SELECT rowid FROM verbatim_fts WHERE verbatim_fts MATCH ?)",
        (session_id, content_hash),
    ).fetchone()
    if existing:
        conn.close()
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
    conn.commit()
    conn.close()
    return row_id


def store_transcript(
    db_path: str,
    transcript_path: str | Path,
    scope: str = "project",
    source_agent: str = "transcript",
) -> dict[str, int]:
    """Ingest an entire JSONL transcript into verbatim storage."""
    path = Path(transcript_path)
    if not path.exists():
        return {"stored": 0, "skipped": 0, "error": "file not found"}

    stats = {"stored": 0, "skipped": 0}
    session_id = path.stem  # Use filename as session ID

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        role = entry.get("role", "")
        content = entry.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            )

        if not content or len(content) < 20:
            stats["skipped"] += 1
            continue

        row_id = store_verbatim(db_path, session_id, role, content, scope, source_agent)
        if row_id:
            stats["stored"] += 1
        else:
            stats["skipped"] += 1

    return stats


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
        # Merge: dedupe by content hash
        seen = {r["content"][:100] for r in results}
        for vr in vector_results:
            if vr["content"][:100] not in seen:
                results.append(vr)
                seen.add(vr["content"][:100])

    # Sort by score descending, limit
    results.sort(key=lambda x: -x.get("score", 0))
    return results[:limit]


def _search_fts(db_path: str, query: str, scope: str | None, limit: int) -> list[dict]:
    """FTS5 keyword search over verbatim memories."""
    conn = _connect(db_path)
    # Clean query for FTS5
    clean_query = " ".join(w for w in query.split() if len(w) > 2)
    if not clean_query:
        conn.close()
        return []

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

    conn.close()
    return [
        {"id": r["id"], "session_id": r["session_id"], "role": r["role"],
         "content": r["content"], "scope": r["scope"], "timestamp": r["timestamp"],
         "score": abs(r["score"]) if r["score"] else 0, "source": "fts"}
        for r in rows
    ]


def _search_vector(query: str, scope: str | None, limit: int) -> list[dict]:
    """Qdrant semantic search over verbatim memories."""
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
            {"content": h["payload"].get("content", ""), "scope": h["payload"].get("scope", ""),
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

    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT id, content, scope, session_id, role FROM verbatim_memories WHERE embedding_synced = 0 LIMIT ?",
        (batch_size,),
    ).fetchall()

    if not rows:
        conn.close()
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
            conn.close()
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
        conn.close()
        return {"synced": 0, "error": str(e)}

    # Upsert to Qdrant
    points = []
    for i, (row, emb) in enumerate(zip(rows, embeddings)):
        points.append({
            "id": row["id"],
            "vector": emb,
            "payload": {
                "content": row["content"][:2000],
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
        conn.close()
        return {"synced": 0, "error": str(e)}

    # Mark as synced
    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"UPDATE verbatim_memories SET embedding_synced = 1 WHERE id IN ({placeholders})", ids)
    conn.commit()
    conn.close()

    return {"synced": len(rows)}
