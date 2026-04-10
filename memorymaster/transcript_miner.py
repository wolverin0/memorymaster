"""Transcript miner — parse Claude Code JSONL transcripts into claims.

Reads session transcripts and ingests assistant messages as claims,
similar to MemPalace's convo_miner.py but for MemoryMaster's claim DB.

Usage:
    memorymaster mine-transcript --input ~/.claude/projects/.../transcripts/
    memorymaster mine-transcript --input session.jsonl --scope project:impulsa
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Credential detection delegated to the canonical filter in
# memorymaster.security — single source of truth.
from memorymaster.security import redact_text as _redact_text


def _contains_sensitive(text: str) -> bool:
    """Return True if text contains any credential the canonical filter catches."""
    _, findings = _redact_text(text)
    return bool(findings)

# Patterns indicating valuable content in assistant messages
VALUABLE_PATTERNS = [
    re.compile(r"(?i)(?:the |root )cause (?:was|is|turned out)"),
    re.compile(r"(?i)(?:decided|chose|going with|switched to)"),
    re.compile(r"(?i)(?:bug|fix|issue|error) (?:was|is) (?:caused|due|because)"),
    re.compile(r"(?i)(?:workaround|solution|trick) (?:is|was) to"),
    re.compile(r"(?i)(?:never|always|must|don.t) .{5,30} (?:because|or else|otherwise)"),
    re.compile(r"(?i)(?:architecture|pattern|approach) (?:is|uses|follows)"),
    re.compile(r"(?i)(?:configured|set up|installed|deployed)"),
]


def _extract_text(content: Any) -> str:
    """Extract text from message content (string or content blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            c.get("text", "") for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    return ""


def mine_transcript(
    transcript_path: str | Path,
    db_path: str,
    scope: str = "project",
    min_length: int = 50,
    max_claims: int = 100,
) -> dict[str, int]:
    """Parse a JSONL transcript and ingest valuable assistant messages as claims.

    Returns: {scanned, ingested, skipped, duplicates}
    """
    import sqlite3

    path = Path(transcript_path)
    if not path.exists():
        return {"scanned": 0, "ingested": 0, "skipped": 0, "duplicates": 0, "error": "file not found"}

    stats = {"scanned": 0, "ingested": 0, "skipped": 0, "duplicates": 0}

    conn = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc).isoformat()

    # Read transcript
    entries = []
    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = sorted(path.glob("*.jsonl"))
    else:
        return {"scanned": 0, "ingested": 0, "skipped": 0, "duplicates": 0, "error": "invalid path"}

    for f in files:
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except Exception:
            continue

    for entry in entries:
        role = entry.get("role", "")
        if role != "assistant":
            continue

        text = _extract_text(entry.get("content", ""))
        if not text or len(text) < min_length:
            continue

        stats["scanned"] += 1

        # Skip sensitive content
        if _contains_sensitive(text):
            stats["skipped"] += 1
            continue

        # Check if this has valuable patterns
        has_value = any(p.search(text) for p in VALUABLE_PATTERNS)
        if not has_value and len(text) < 200:
            stats["skipped"] += 1
            continue

        # Content hash for dedup
        text_hash = hashlib.sha256(text[:500].strip().lower().encode()).hexdigest()[:16]
        idem_key = f"transcript-{text_hash}"

        existing = conn.execute(
            "SELECT id FROM claims WHERE idempotency_key = ?", (idem_key,)
        ).fetchone()
        if existing:
            stats["duplicates"] += 1
            continue

        # Truncate to reasonable size
        claim_text = text[:500]

        conn.execute(
            """INSERT INTO claims (text, idempotency_key, normalized_text, claim_type,
               subject, predicate, scope, status, confidence,
               source_agent, created_at, updated_at, tier, version, visibility,
               valid_from)
               VALUES (?, ?, ?, 'fact', 'session', 'observation', ?, 'candidate', 0.5,
               'transcript-miner', ?, ?, 'working', 1, 'public', ?)""",
            (claim_text, idem_key, claim_text.lower(), scope, now, now, now),
        )
        stats["ingested"] += 1

        if stats["ingested"] >= max_claims:
            break

    conn.commit()
    conn.close()

    logger.info(
        "Transcript mine: %d scanned, %d ingested, %d skipped, %d duplicates",
        stats["scanned"], stats["ingested"], stats["skipped"], stats["duplicates"],
    )
    return stats
