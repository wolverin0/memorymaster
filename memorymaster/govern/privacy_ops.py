"""Read-only, inventory-driven privacy export/erase planning."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
from typing import Any


@dataclass(frozen=True, slots=True)
class PrivacySelector:
    principal: str
    tenant_id: str | None = None
    scope: str | None = None

    def __post_init__(self) -> None:
        principal = self.principal.strip()
        if not principal:
            raise ValueError("privacy selector principal must not be blank")
        object.__setattr__(self, "principal", principal)
        object.__setattr__(self, "tenant_id", (self.tenant_id or "").strip() or None)
        object.__setattr__(self, "scope", (self.scope or "").strip() or None)


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (name,),
    ).fetchone() is not None


def _claim_inventory(connection: sqlite3.Connection, selector: PrivacySelector) -> dict[str, Any]:
    clauses = ["(source_agent = ? OR holder = ?)"]
    params: list[Any] = [selector.principal, selector.principal]
    if selector.tenant_id:
        clauses.append("tenant_id = ?")
        params.append(selector.tenant_id)
    if selector.scope:
        clauses.append("scope = ?")
        params.append(selector.scope)
    where = " AND ".join(clauses)
    if selector.tenant_id is None:
        tenants = {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT COALESCE(tenant_id, '') FROM claims WHERE (source_agent = ? OR holder = ?)",
                (selector.principal, selector.principal),
            )
        }
        if len(tenants) > 1:
            return {
                "surface": "primary_db",
                "status": "BLOCKED-EXTERNAL",
                "reason": "principal spans multiple tenant boundaries; tenant_id is required",
            }
    ids = [int(row[0]) for row in connection.execute(f"SELECT id FROM claims WHERE {where}", params)]
    if not ids:
        return {"surface": "primary_db", "status": "NOT_FOUND", "claims": 0, "citations": 0, "events": 0}
    placeholders = ",".join("?" for _ in ids)
    citations = connection.execute(f"SELECT COUNT(*) FROM citations WHERE claim_id IN ({placeholders})", ids).fetchone()[0]
    events = connection.execute(f"SELECT COUNT(*) FROM events WHERE claim_id IN ({placeholders})", ids).fetchone()[0]
    return {"surface": "primary_db", "status": "FOUND", "claims": len(ids), "citations": citations, "events": events}


def _verbatim_inventory(connection: sqlite3.Connection, selector: PrivacySelector) -> dict[str, Any]:
    if not _has_table(connection, "verbatim_memories"):
        return {"surface": "verbatim", "status": "NOT_FOUND", "rows": 0}
    clauses = ["source_agent = ?"]
    params: list[Any] = [selector.principal]
    if selector.scope:
        clauses.append("scope = ?")
        params.append(selector.scope)
    count = connection.execute(
        f"SELECT COUNT(*) FROM verbatim_memories WHERE {' AND '.join(clauses)}",
        params,
    ).fetchone()[0]
    return {"surface": "verbatim", "status": "FOUND" if count else "NOT_FOUND", "rows": count}


def _path_surface(workspace: Path, name: str, relative: str) -> dict[str, Any]:
    root = workspace / relative
    count = sum(1 for path in root.rglob("*") if path.is_file()) if root.exists() else 0
    return {
        "surface": name,
        "status": "BLOCKED-EXTERNAL" if count else "NOT_FOUND",
        "objects": count,
        "reason": "objects are not reliably principal-attributable" if count else "surface absent",
    }


def build_privacy_plan(
    *,
    db_target: str | Path,
    workspace: str | Path,
    selector: PrivacySelector,
) -> dict[str, Any]:
    if str(db_target).strip().lower().startswith(("postgres://", "postgresql://")):
        return {
            "schema_version": "memorymaster.privacy-plan.v1",
            "dry_run": True,
            "selector": {"principal": selector.principal, "tenant_id": selector.tenant_id, "scope": selector.scope},
            "surfaces": [{
                "surface": "primary_db",
                "status": "BLOCKED-EXTERNAL",
                "reason": "Postgres privacy inventory requires a disposable restricted-role runtime",
            }],
            "complete": False,
            "mutation_count": 0,
        }
    db_path = Path(db_target).resolve()
    workspace_path = Path(workspace).resolve()
    with _connect_read_only(db_path) as connection:
        surfaces = [
            _claim_inventory(connection, selector),
            _verbatim_inventory(connection, selector),
            _path_surface(workspace_path, "artifacts", "artifacts"),
            _path_surface(workspace_path, "spool", ".memorymaster/spool"),
            _path_surface(workspace_path, "wiki", "obsidian-vault"),
        ]
        cache_rows = connection.execute("SELECT COUNT(*) FROM query_cache").fetchone()[0] if _has_table(connection, "query_cache") else 0
    surfaces.append({
        "surface": "query_cache",
        "status": "BLOCKED-EXTERNAL" if cache_rows else "NOT_FOUND",
        "rows": cache_rows,
        "reason": "cache rows lack principal attribution" if cache_rows else "cache empty",
    })
    surfaces.append({
        "surface": "qdrant",
        "status": "BLOCKED-EXTERNAL",
        "configured": bool(os.environ.get("QDRANT_URL", "").strip()),
        "reason": "requires disposable authenticated/TLS inventory and deletion evidence",
    })
    backups = _path_surface(workspace_path, "backups", ".memorymaster/snapshots")
    backups["disposition"] = "expire_by_policy"
    surfaces.append(backups)
    complete = all(row["status"] in {"FOUND", "NOT_FOUND"} for row in surfaces)
    return {
        "schema_version": "memorymaster.privacy-plan.v1",
        "dry_run": True,
        "selector": {"principal": selector.principal, "tenant_id": selector.tenant_id, "scope": selector.scope},
        "surfaces": surfaces,
        "complete": complete,
        "mutation_count": 0,
    }
