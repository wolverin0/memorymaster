"""Durable session-to-scope binding with fail-closed scope resolution."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from memorymaster.core.scope_utils import canonicalize_slug
from memorymaster.core.security import redact_text
from memorymaster.stores._storage_shared import open_conn

DEFAULT_BINDING_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_BINDING_TTL_SECONDS = 30 * 24 * 60 * 60
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,95}$")
_PROJECT_SCOPE_RE = re.compile(r"^project:[a-z0-9][a-z0-9_-]{0,95}$")
_BINDING_SOURCES = frozenset({"explicit", "verified_workspace", "default_user"})


@dataclass(frozen=True, slots=True)
class SessionScopeBinding:
    id: int
    session_hash: str
    source_agent: str
    platform: str
    scope: str
    workspace_slug: str | None
    task_label: str | None
    binding_source: str
    created_at: str
    last_seen_at: str
    expires_at: str
    ended_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    scope: str
    scope_source: str
    session_hash: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def hash_session_id(session_id: str) -> str:
    value = str(session_id or "").strip()
    if not value or len(value) > 512:
        raise ValueError("session_id must contain 1 to 512 characters")
    return hashlib.sha256(f"memorymaster-session-v1\0{value}".encode()).hexdigest()


def _identity(value: str, field: str) -> str:
    cleaned = str(value or "").strip()
    if not _IDENTITY_RE.fullmatch(cleaned):
        raise ValueError(f"{field} contains unsupported characters")
    return cleaned


def validate_scope(scope: str, *, allow_global: bool = True) -> str:
    cleaned = str(scope or "").strip().lower()
    valid = cleaned == "user" or bool(_PROJECT_SCOPE_RE.fullmatch(cleaned))
    if cleaned == "global" and allow_global:
        valid = True
    if not valid:
        raise ValueError("scope must be user, project:<slug>, or explicitly authorized global")
    return cleaned


def _safe_task_label(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    cleaned = str(value).strip()
    if len(cleaned) > 120:
        raise ValueError("task_label must be at most 120 characters")
    _redacted, findings = redact_text(cleaned)
    if findings:
        raise ValueError("task_label contains sensitive content")
    return cleaned


def _binding(row: Any) -> SessionScopeBinding:
    return SessionScopeBinding(**dict(row))


def _missing_table(exc: sqlite3.OperationalError) -> bool:
    return "no such table: session_scope_bindings" in str(exc).lower()


class SessionScopeRepository:
    """SQLite repository that never persists raw session identifiers."""

    def __init__(self, db_path: str | Path) -> None:
        target = str(db_path)
        if "://" in target:
            raise ValueError("session scope bindings require the SQLite authority")
        self.db_path = target

    def _connect(self) -> sqlite3.Connection:
        return open_conn(self.db_path)

    def get_active(
        self,
        session_id: str,
        *,
        source_agent: str,
        platform: str,
        now: datetime | None = None,
    ) -> SessionScopeBinding | None:
        digest = hash_session_id(session_id)
        current = _iso(now or _utc_now())
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """SELECT * FROM session_scope_bindings
                       WHERE session_hash=? AND source_agent=? AND platform=?
                         AND ended_at IS NULL AND expires_at>?
                       ORDER BY id DESC LIMIT 1""",
                    (digest, _identity(source_agent, "source_agent"),
                     _identity(platform, "platform"), current),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if _missing_table(exc):
                return None
            raise
        return _binding(row) if row is not None else None

    def bind(
        self,
        session_id: str,
        *,
        scope: str,
        source_agent: str,
        platform: str,
        binding_source: str,
        workspace_slug: str | None = None,
        task_label: str | None = None,
        ttl_seconds: int = DEFAULT_BINDING_TTL_SECONDS,
        now: datetime | None = None,
        replace: bool = False,
    ) -> SessionScopeBinding:
        values = self._validated_bind_values(
            session_id, scope, source_agent, platform, binding_source,
            workspace_slug, task_label, ttl_seconds, now,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._end_expired(conn, values)
            row = self._active_row(conn, values)
            same_binding = row is not None and (
                str(row["scope"]) == values[3]
                and str(row["binding_source"]) == values[5]
            )
            if row is not None and not same_binding and not replace:
                raise ValueError("session already has a different active binding")
            if same_binding:
                conn.execute(
                    """UPDATE session_scope_bindings
                       SET last_seen_at=?, expires_at=?, task_label=COALESCE(?, task_label)
                       WHERE id=?""",
                    (values[8], values[9], values[6], int(row["id"])),
                )
                binding_id = int(row["id"])
            else:
                if row is not None:
                    conn.execute(
                        "UPDATE session_scope_bindings SET ended_at=? WHERE id=?",
                        (values[8], int(row["id"])),
                    )
                binding_id = self._insert(conn, values)
            conn.commit()
            result = conn.execute(
                "SELECT * FROM session_scope_bindings WHERE id=?", (binding_id,)
            ).fetchone()
        return _binding(result)

    @staticmethod
    def _validated_bind_values(
        session_id: str, scope: str, source_agent: str, platform: str,
        binding_source: str, workspace_slug: str | None, task_label: str | None,
        ttl_seconds: int, now: datetime | None,
    ) -> tuple[Any, ...]:
        if binding_source not in _BINDING_SOURCES:
            raise ValueError("unsupported binding_source")
        if not 60 <= int(ttl_seconds) <= MAX_BINDING_TTL_SECONDS:
            raise ValueError("ttl_seconds must be between 60 and 2592000")
        current = now or _utc_now()
        slug = canonicalize_slug(workspace_slug) if workspace_slug else None
        return (
            hash_session_id(session_id), _identity(source_agent, "source_agent"),
            _identity(platform, "platform"), validate_scope(scope), slug,
            binding_source, _safe_task_label(task_label), _iso(current),
            _iso(current), _iso(current + timedelta(seconds=int(ttl_seconds))),
        )

    @staticmethod
    def _end_expired(conn: sqlite3.Connection, values: tuple[Any, ...]) -> None:
        conn.execute(
            """UPDATE session_scope_bindings SET ended_at=?
               WHERE session_hash=? AND source_agent=? AND platform=?
                 AND ended_at IS NULL AND expires_at<=?""",
            (values[8], values[0], values[1], values[2], values[8]),
        )

    @staticmethod
    def _active_row(conn: sqlite3.Connection, values: tuple[Any, ...]) -> Any:
        return conn.execute(
            """SELECT * FROM session_scope_bindings
               WHERE session_hash=? AND source_agent=? AND platform=? AND ended_at IS NULL
               ORDER BY id DESC LIMIT 1""",
            values[:3],
        ).fetchone()

    @staticmethod
    def _insert(conn: sqlite3.Connection, values: tuple[Any, ...]) -> int:
        cursor = conn.execute(
            """INSERT INTO session_scope_bindings
               (session_hash, source_agent, platform, scope, workspace_slug,
                binding_source, task_label, created_at, last_seen_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            values,
        )
        return int(cursor.lastrowid)

    def end(
        self,
        session_id: str,
        *,
        source_agent: str | None = None,
        platform: str | None = None,
        now: datetime | None = None,
    ) -> int:
        clauses = ["session_hash=?", "ended_at IS NULL"]
        params: list[Any] = [hash_session_id(session_id)]
        if source_agent:
            clauses.append("source_agent=?")
            params.append(_identity(source_agent, "source_agent"))
        if platform:
            clauses.append("platform=?")
            params.append(_identity(platform, "platform"))
        params.append(_iso(now or _utc_now()))
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE session_scope_bindings SET ended_at=? WHERE {' AND '.join(clauses)}",
                (params[-1], *params[:-1]),
            )
            conn.commit()
            return int(cursor.rowcount)

    def history(self, session_id: str, *, limit: int = 100) -> list[SessionScopeBinding]:
        bounded = min(max(int(limit), 1), 100)
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """SELECT * FROM session_scope_bindings WHERE session_hash=?
                       ORDER BY id DESC LIMIT ?""",
                    (hash_session_id(session_id), bounded),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if _missing_table(exc):
                return []
            raise
        return [_binding(row) for row in rows]

    def list_active(self, *, limit: int = 100, now: datetime | None = None) -> list[SessionScopeBinding]:
        bounded = min(max(int(limit), 1), 100)
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """SELECT * FROM session_scope_bindings
                       WHERE ended_at IS NULL AND expires_at>?
                       ORDER BY last_seen_at DESC LIMIT ?""",
                    (_iso(now or _utc_now()), bounded),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if _missing_table(exc):
                return []
            raise
        return [_binding(row) for row in rows]


class SessionScopeResolver:
    """Resolve explicit, persisted, verified-workspace, then user scope."""

    def __init__(self, db_path: str | Path) -> None:
        self.repository = SessionScopeRepository(db_path)

    def resolve(
        self,
        *,
        session_id: str | None,
        explicit_scope: str | None,
        workspace: Path | None,
        source_agent: str,
        platform: str,
        allowed_scopes: Iterable[str] | None = None,
        task_label: str | None = None,
        now: datetime | None = None,
    ) -> ResolvedScope:
        allowed = {validate_scope(item) for item in allowed_scopes or ()}
        if explicit_scope and explicit_scope.strip() not in {"project"}:
            scope = validate_scope(explicit_scope)
            self._authorize(scope, allowed)
            binding = self._bind(
                session_id, scope, "explicit", workspace, source_agent,
                platform, task_label, now, replace=True,
            )
            return ResolvedScope(scope, "explicit", binding.session_hash if binding else None)
        existing = self._existing(session_id, source_agent, platform, now)
        if existing is not None:
            self._authorize(existing.scope, allowed)
            self.repository.bind(
                session_id or "", scope=existing.scope, source_agent=source_agent,
                platform=platform, binding_source=existing.binding_source,
                workspace_slug=existing.workspace_slug, task_label=task_label, now=now,
            )
            return ResolvedScope(existing.scope, "session_binding", existing.session_hash)
        scope, source = self._workspace_or_user(workspace)
        self._authorize(scope, allowed)
        binding = self._bind(
            session_id, scope, source, workspace, source_agent,
            platform, task_label, now, replace=False,
        )
        return ResolvedScope(scope, source, binding.session_hash if binding else None)

    def _existing(
        self, session_id: str | None, source_agent: str, platform: str,
        now: datetime | None,
    ) -> SessionScopeBinding | None:
        if not session_id:
            return None
        return self.repository.get_active(
            session_id, source_agent=source_agent, platform=platform, now=now
        )

    @staticmethod
    def _workspace_or_user(workspace: Path | None) -> tuple[str, str]:
        if workspace is not None and workspace.is_dir() and workspace.name:
            return f"project:{canonicalize_slug(workspace.name)}", "verified_workspace"
        return "user", "default_user"

    @staticmethod
    def _authorize(scope: str, allowed: set[str]) -> None:
        if allowed and scope not in allowed:
            raise PermissionError("scope is outside the authorized scopes")

    def _bind(
        self, session_id: str | None, scope: str, source: str,
        workspace: Path | None, source_agent: str, platform: str,
        task_label: str | None, now: datetime | None, replace: bool,
    ) -> SessionScopeBinding | None:
        if not session_id:
            return None
        return self.repository.bind(
            session_id, scope=scope, source_agent=source_agent, platform=platform,
            binding_source=source, workspace_slug=workspace.name if workspace else None,
            task_label=task_label, now=now, replace=replace,
        )
