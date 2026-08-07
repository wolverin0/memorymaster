"""Configuration loading for the standalone Hermes MemoryMaster provider."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


CONFIG_NAME = "memorymaster-provider.json"
TOKEN_ENV = "MEMORYMASTER_HERMES_MCP_TOKEN"
ENDPOINT_ENV = "MEMORYMASTER_HERMES_MCP_URL"


def _positive_number(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    endpoint: str = ""
    token: str = ""
    outbox_path: Path = Path("memorymaster-outbox.db")
    replica_db_path: Path | None = None
    replica_workspace: Path | None = None
    default_scope: str = "user"
    request_timeout_seconds: float = 0.35
    max_pending: int = 1000
    max_pending_bytes: int = 16 * 1024 * 1024
    max_attempts: int = 5
    retry_base_seconds: float = 2.0
    retry_cap_seconds: float = 6 * 60 * 60
    circuit_failure_threshold: int = 5
    circuit_reset_seconds: float = 120.0
    recall_cache_seconds: float = 120.0
    shutdown_drain_seconds: float = 2.0
    worker_enabled: bool = True

    def __post_init__(self) -> None:
        _endpoint(self.endpoint)

    @classmethod
    def load(cls, hermes_home: str | Path) -> "ProviderConfig":
        home = Path(hermes_home).expanduser().resolve()
        raw = _read_config(home / CONFIG_NAME)
        endpoint = os.environ.get(ENDPOINT_ENV, str(raw.get("endpoint", ""))).strip()
        token = os.environ.get(TOKEN_ENV, "").strip()
        outbox = _under_home(home, raw.get("outbox", "memorymaster-outbox.db"))
        replica = _optional_path(raw.get("replica_db"))
        workspace = _optional_path(raw.get("replica_workspace"))
        return cls(
            endpoint=endpoint,
            token=token,
            outbox_path=outbox,
            replica_db_path=replica,
            replica_workspace=workspace,
            default_scope=_scope(raw.get("default_scope")),
            request_timeout_seconds=_positive_number(raw.get("request_timeout_seconds"), 0.35),
            max_pending=_positive_int(raw.get("max_pending"), 1000),
            max_pending_bytes=_positive_int(raw.get("max_pending_bytes"), 16 * 1024 * 1024),
            shutdown_drain_seconds=_positive_number(raw.get("shutdown_drain_seconds"), 2.0),
        )

    def public_config(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "outbox": str(self.outbox_path),
            "replica_db": str(self.replica_db_path) if self.replica_db_path else "",
            "replica_workspace": str(self.replica_workspace) if self.replica_workspace else "",
            "default_scope": self.default_scope,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_pending": self.max_pending,
            "max_pending_bytes": self.max_pending_bytes,
            "shutdown_drain_seconds": self.shutdown_drain_seconds,
        }


def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{CONFIG_NAME} must contain a JSON object")
    return value


def _under_home(home: Path, value: Any) -> Path:
    candidate = Path(str(value)).expanduser()
    resolved = candidate.resolve() if candidate.is_absolute() else (home / candidate).resolve()
    if home not in resolved.parents and resolved != home:
        raise ValueError("Hermes provider outbox must remain under HERMES_HOME")
    return resolved


def _optional_path(value: Any) -> Path | None:
    text = str(value or "").strip()
    return Path(text).expanduser().resolve() if text else None


def _scope(value: Any) -> str:
    scope = str(value or "user").strip()
    if scope == "global" or not (scope == "user" or scope.startswith("project:")):
        raise ValueError("default_scope must be user or project:<slug>; global is forbidden")
    return scope


def _endpoint(value: Any) -> str:
    endpoint = str(value or "").strip()
    if not endpoint:
        return ""
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("endpoint must be an HTTP(S) URL without credentials, query, or fragment")
    return endpoint
