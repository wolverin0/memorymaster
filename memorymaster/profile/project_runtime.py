"""Explicit opt-in configuration and bounded project projection consumers."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from memorymaster.core.generated_artifacts import GENERATED_CLOSER, GENERATED_OPENER

PREFIX = "MEMORYMASTER_PROJECT_PROJECTION_"
_refresh_cache: dict[tuple, tuple] = {}
_connections: dict[str, sqlite3.Connection] = {}
_rotation = 0


def active(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def load_project_configs() -> list:
    """Parse an explicit pilot allowlist. No path discovery or store initialization."""
    if not active(PREFIX + "ENABLED"):
        return []
    from memorymaster.profile.project_projection import ProjectProjectionConfig

    try:
        raw = os.environ.get(PREFIX + "PROJECTS", "[]")
        if len(raw.encode()) > 32768:
            return []
        mappings = json.loads(raw)
        epoch = os.environ.get(PREFIX + "EPOCH", "").strip()
        output = os.environ.get(PREFIX + "DIR", "")
        state = os.environ.get("MEMORYMASTER_PROJECT_SESSION_STATE_DIR", "")
        ledger = os.environ.get("MEMORYMASTER_PROJECT_LEDGER_TASKS_DIR", "")
        if (not isinstance(mappings, list) or not 1 <= len(mappings) <= 3 or not epoch
                or len(epoch) > 128 or not output or not state
                or not Path(output).is_absolute() or not Path(state).is_absolute()
                or (ledger and not Path(ledger).is_absolute())):
            return []
        configs = []
        seen = set()
        for row in mappings:
            # Null tenant is a deliberate legacy boundary, not a default across tenants.
            if "tenant_id" not in row or not Path(row["db_path"]).is_absolute():
                return []
            if not Path(row["project_root"]).is_absolute():
                return []
            identity = (row["context_key"], row["scope"])
            if identity in seen:
                return []
            seen.add(identity)
            configs.append(ProjectProjectionConfig(
                db_path=Path(row["db_path"]), scope=row["scope"], tenant_id=row["tenant_id"],
                context_key=row["context_key"], epoch=epoch, project_root=Path(row["project_root"]),
                output_root=Path(output), state_root=Path(state),
                ledger_root=Path(ledger) if ledger else None, ledger_repo=row.get("ledger_repo"),
                ledger_filtered=row.get("ledger_filtered") is True,
                aliases=tuple(row.get("aliases", ())), enabled=True))
        return configs
    except (ValueError, KeyError, TypeError):
        return []


def resolve_projection_scope(config, cwd) -> str | None:
    """Both scope resolvers and the actual repository owner must agree."""
    from memorymaster.core.scope_utils import (
        _repository_owner,
        resolve_transcript_workspace_scope,
        scope_from_cwd_or_user,
    )

    try:
        path = Path(cwd).resolve(strict=True)
        if not path.is_dir():
            return None
        strict = resolve_transcript_workspace_scope(str(path))
        generic = scope_from_cwd_or_user(str(path))
        if strict != generic or strict not in (config.scope, *config.aliases):
            return None
        for parent in (path, *list(path.parents)[:12]):
            marker = parent / ".git"
            if marker.exists():
                owner = _repository_owner(parent)
                if owner is None or owner.resolve() != config.project_root.resolve(strict=True):
                    return None
                # Linked worktrees must have a valid backlink; generic resolution alone is insufficient.
                if marker.is_file():
                    if marker.stat().st_size > 4096:
                        return None
                    pointer = marker.read_text(encoding="utf-8").strip()
                    if not pointer.startswith("gitdir:"):
                        return None
                    gitdir = (parent / pointer.partition(":")[2].strip()).resolve()
                    backlink = gitdir / "gitdir"
                    if (not backlink.is_file() or backlink.stat().st_size > 4096
                            or Path(backlink.read_text(encoding="utf-8").strip()).resolve() != marker.resolve()):
                        return None
                return config.scope
        return None
    except (OSError, ValueError, RuntimeError):
        return None


def config_for_cwd(cwd):
    matches = [cfg for cfg in load_project_configs() if resolve_projection_scope(cfg, cwd)]
    return matches[0] if len(matches) == 1 else None


def compose_session_context(payload: str, delta: str = "", profile: str = "") -> str:
    """Embed the shared bytes intact, with one envelope and complete optional lines."""
    if len(payload.encode("utf-8")) > 2200 or len(delta.encode("utf-8")) > 500:
        raise ValueError("consumer_budget")
    if GENERATED_OPENER in payload or GENERATED_CLOSER in payload:
        raise ValueError("nested_envelope")
    parts = [payload]
    if delta:
        parts.append(delta)
    profile_lines = [line for line in profile.splitlines()
                     if line.strip() and not line.lstrip().startswith("<!--")]
    omitted = len(profile_lines)
    for line in profile_lines:
        candidate = "\n".join([GENERATED_OPENER, *parts, line,
                                f"profile_omitted={omitted - 1}", GENERATED_CLOSER])
        if len(candidate) <= 3000:
            parts.append(line)
            omitted -= 1
        else:
            break
    context = "\n".join([GENERATED_OPENER, *parts, f"profile_omitted={omitted}", GENERATED_CLOSER])
    if len(context) > 3000:
        raise ValueError("consumer_budget")
    return context


def session_start_context(data: dict, *, profile_loader=None) -> str | None:
    if not active(PREFIX + "ENABLED") or not active(PREFIX + "SESSION_START"):
        return None
    config = config_for_cwd(data.get("cwd") or os.getcwd())
    if config is None:
        return None
    from memorymaster.profile.project_projection import read_project_projection

    projection = read_project_projection(config)
    if projection is None:
        return compose_session_context("Project context unavailable; refresh pending.")
    delta = ""
    if active("MEMORYMASTER_PROJECT_SESSION_DELTA"):
        from memorymaster.profile.project_sessions import record_project_session_start

        result = record_project_session_start(config, str(data.get("session_id") or ""),
                                              projection=projection.manifest)
        delta = (result["delta"].text if result.get("delta")
                 else "Prior-session continuity unavailable: " + result["status"])
    profile = profile_loader() if profile_loader is not None else ""
    return compose_session_context(projection.payload, delta, profile)


def record_session_end(data: dict) -> bool:
    if not active(PREFIX + "ENABLED") or not active("MEMORYMASTER_PROJECT_SESSION_DELTA"):
        return False
    config = config_for_cwd(data.get("cwd") or os.getcwd())
    if config is None:
        return False
    from memorymaster.profile.project_sessions import record_project_session_end

    return record_project_session_end(config, str(data.get("session_id") or ""), end_at=data.get("end_at"))


def cheap_fingerprint(config) -> tuple:
    """Persistent data_version plus bounded file metadata before any source scan."""
    key = str(config.db_path.resolve())
    connection = _connections.get(key)
    if connection is None:
        if len(_connections) >= 3:
            _, previous = _connections.popitem()
            previous.close()
        connection = sqlite3.connect(config.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.025)
        connection.execute("PRAGMA query_only=ON")
        _connections[key] = connection
    data_version = connection.execute("PRAGMA data_version").fetchone()[0]
    def stat(path):
        try:
            value = path.stat()
            return (value.st_mtime_ns, value.st_size)
        except FileNotFoundError:
            return None
    files = [stat(config.db_path), stat(Path(str(config.db_path) + "-wal"))]
    if config.ledger_root:
        # Only configured JSON records, bounded before materializing a directory list.
        for index, path in enumerate(sorted(config.ledger_root.glob("*.json"))):
            if index >= 1024:
                raise ValueError("ledger_capacity")
            files.append((path.name, stat(path)))
    # An elapsed second can cross valid_from/valid_until even without a database writer.
    # Cache stores the next temporal boundary separately after each full source read.
    return (data_version, *files)


def _refresh_one(config) -> dict:
    from memorymaster.profile.project_projection import build_project_projection, publish_project_projection

    projection = build_project_projection(config)
    if not projection.manifest.get("complete", projection.manifest.get("source_complete", False)):
        return {"status": "pending"}
    published = publish_project_projection(config, projection)
    return {"status": "ok" if published else "pending"}


def refresh_project_projections(*, skip: bool = False) -> dict:
    if not active(PREFIX + "ENABLED") or not active(PREFIX + "REFRESH"):
        return {"status": "inactive"}
    global _rotation
    configs = load_project_configs()
    if not configs:
        return {"status": "inactive"}
    start = time.monotonic()
    offset = _rotation % len(configs)
    _rotation += 1
    results = []
    for config in configs[offset:] + configs[:offset]:
        if time.monotonic() - start >= .750:
            results.append({"status": "pending", "reason": "tick_budget"})
            break
        key = (config.context_key, config.scope, config.epoch)
        try:
            cheap = cheap_fingerprint(config)
            cached = _refresh_cache.get(key)
            # Expiry is conservative: force a validation at least each minute, including temporal changes.
            if skip and cached and cached[0] == cheap and time.monotonic() < cached[1]:
                results.append({"status": "unchanged"})
                continue
            result = _refresh_one(config)
            if result["status"] == "ok":
                _refresh_cache[key] = (cheap, time.monotonic() + 60)
            results.append(result)
        except (OSError, ValueError, sqlite3.Error):
            results.append({"status": "pending", "reason": "source_unavailable"})
    return {"status": "ok" if all(r["status"] in {"ok", "unchanged"} for r in results) else "pending",
            "projects": results, "checked_at": datetime.now(timezone.utc).isoformat()}
