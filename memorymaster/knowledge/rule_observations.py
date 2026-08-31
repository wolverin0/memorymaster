"""Independent root-session lineage for mined behavioral rules."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any


_SAFE_REF = re.compile(r"[^A-Za-z0-9._:-]+")
_HUMAN_KINDS = {"human", "mixed"}
_DDL = """
CREATE TABLE IF NOT EXISTS rule_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_fingerprint TEXT NOT NULL,
    provider TEXT NOT NULL,
    root_session_hash TEXT NOT NULL,
    project_scope TEXT NOT NULL,
    session_kind TEXT NOT NULL CHECK(session_kind IN ('human','mixed','subagent','automation')),
    is_independent INTEGER NOT NULL CHECK(is_independent IN (0,1)),
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 1 CHECK(event_count > 0),
    evidence_hash TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    UNIQUE(rule_fingerprint, provider, root_session_hash)
);
CREATE INDEX IF NOT EXISTS idx_rule_observations_support
    ON rule_observations(rule_fingerprint, is_independent, project_scope);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _safe_scope(value: str) -> str:
    scope = (value or "global").strip()
    if scope in {"user", "global"}:
        return scope
    if scope.startswith("project:"):
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", scope[8:]).strip("-")
        return f"project:{slug or 'unknown'}"
    return "project:unknown"


def ensure_rule_observation_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()


def record_rule_observation(
    conn: sqlite3.Connection,
    *,
    rule_fingerprint: str,
    provider: str,
    root_session_id: str,
    project_scope: str,
    source_ref: str,
    evidence_hash: str,
    session_kind: str = "human",
) -> None:
    """Upsert activity while counting one independent root at most once."""
    fingerprint = str(rule_fingerprint).strip()
    if not fingerprint or not root_session_id:
        raise ValueError("rule fingerprint and root session are required")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", evidence_hash):
        raise ValueError("evidence_hash must be sha256 hex")
    ensure_rule_observation_schema(conn)
    kind = session_kind if session_kind in {"human", "mixed", "subagent", "automation"} else "automation"
    now = _now()
    conn.execute(
        """INSERT INTO rule_observations(
               rule_fingerprint,provider,root_session_hash,project_scope,session_kind,
               is_independent,first_observed_at,last_observed_at,event_count,evidence_hash,source_ref
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(rule_fingerprint,provider,root_session_hash) DO UPDATE SET
               last_observed_at=excluded.last_observed_at,
               event_count=rule_observations.event_count+1,
               evidence_hash=excluded.evidence_hash,
               source_ref=excluded.source_ref""",
        (
            fingerprint, (provider or "unknown").strip().lower()[:32],
            _session_hash(root_session_id), _safe_scope(project_scope), kind,
            int(kind in _HUMAN_KINDS), now, now, 1, evidence_hash.lower(),
            _SAFE_REF.sub("-", source_ref or "unknown")[:160],
        ),
    )
    conn.commit()


def observation_support(
    conn: sqlite3.Connection, rule_fingerprint: str, *, scope: str,
) -> dict[str, int | bool]:
    params: list[Any] = [rule_fingerprint]
    where = "rule_fingerprint=? AND is_independent=1"
    if scope.startswith("project:"):
        where += " AND project_scope=?"
        params.append(_safe_scope(scope))
    row = conn.execute(
        f"""SELECT COUNT(DISTINCT root_session_hash),
                   COUNT(DISTINCT CASE WHEN project_scope LIKE 'project:%' THEN project_scope END)
            FROM rule_observations WHERE {where}""",
        tuple(params),
    ).fetchone()
    sessions = int(row[0]) if row else 0
    projects = int(row[1]) if row else 0
    eligible = sessions >= 3 and (not scope.startswith(("user", "global")) or projects >= 2)
    return {"root_sessions": sessions, "projects": projects, "eligible": eligible}


__all__ = ["ensure_rule_observation_schema", "observation_support", "record_rule_observation"]
