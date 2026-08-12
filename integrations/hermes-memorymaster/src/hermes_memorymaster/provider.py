"""Hermes MemoryProvider implementation backed by governed MemoryMaster MCP."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import random
import re
import threading
import time
from pathlib import Path
from typing import Any

from ._compat import HERMES_ABI_AVAILABLE, MemoryProvider
from .backend import (
    BackendAuthError,
    BackendPayloadError,
    BackendScopeError,
    BackendTransientError,
    MCPHttpBackend,
    MemoryMasterBackend,
    ReadOnlyReplicaBackend,
)
from .config import CONFIG_NAME, ProviderConfig
from .outbox import DurableOutbox, OutboxEntry
from .security import sanitize_outbox_text


logger = logging.getLogger(__name__)
CONTRACT = "memorymaster.hermes.capture.v1"
_SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.:-]+")


class MemoryMasterProvider(MemoryProvider):
    def __init__(
        self,
        *,
        config: ProviderConfig | None = None,
        backend: MemoryMasterBackend | None = None,
        replica_backend: Any | None = None,
        clock: Any = time.time,
    ) -> None:
        self.config = config
        self.backend = backend
        self.replica_backend = replica_backend
        self.clock = clock
        self.outbox: DurableOutbox | None = None
        self.session_hash = ""
        self.source_agent = "hermes-memorymaster"
        self.platform = "hermes"
        self.agent_context = "primary"
        self.scope_value = "user"
        self.turn_id = ""
        self.session_lineage: dict[str, Any] = {}
        self._failure_count = 0
        self._circuit_open_until = 0.0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None
        self._prefetch_thread: threading.Thread | None = None
        self._cache: dict[str, tuple[float, str]] = {}

    @property
    def name(self) -> str:
        return "memorymaster"

    def is_available(self) -> bool:
        config = self.config
        if config is None:
            home = Path.home() / ".hermes"
            try:
                config = ProviderConfig.load(home)
            except (OSError, ValueError, json.JSONDecodeError):
                return False
        return bool(
            HERMES_ABI_AVAILABLE
            and config.endpoint
            and config.token
            and importlib.util.find_spec("mcp")
        )

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        hermes_home = Path(str(kwargs.get("hermes_home") or Path.home() / ".hermes"))
        self.config = self.config or ProviderConfig.load(hermes_home)
        self.backend = self.backend or MCPHttpBackend(
            self.config.endpoint,
            self.config.token,
            timeout_seconds=self.config.request_timeout_seconds,
            delivery_timeout_seconds=self.config.delivery_timeout_seconds,
        )
        if self.replica_backend is None and self.config.replica_db_path:
            self.replica_backend = ReadOnlyReplicaBackend(
                self.config.replica_db_path, self.config.replica_workspace
            )
        self.outbox = DurableOutbox(
            self.config.outbox_path,
            max_pending=self.config.max_pending,
            max_pending_bytes=self.config.max_pending_bytes,
            clock=self.clock,
        )
        self._set_session(session_id)
        self.platform = _safe_label(kwargs.get("platform"), "hermes")
        self.agent_context = _safe_label(kwargs.get("agent_context"), "primary")
        identity = _safe_label(kwargs.get("agent_identity"), "memorymaster")
        self.source_agent = f"hermes:{identity}"
        self.scope_value = self.config.default_scope
        if self.config.worker_enabled:
            self._start_worker()

    def system_prompt_block(self) -> str:
        return (
            "MemoryMaster provides governed recall and queued capture. "
            "Use memorymaster_scope before project-specific writes; forgetting is preview-only."
        )

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        del messages
        if self.agent_context != "primary":
            return
        if session_id and _session_hash(session_id) != self.session_hash:
            self._set_session(session_id)
        text = f"User: {user_content.strip()}\nAssistant: {assistant_content.strip()}".strip()
        if text == "User:\nAssistant:":
            return
        self._queue_text(text, origin="turn")

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        del message
        self.turn_id = str(max(0, int(turn_number)))
        if kwargs.get("platform"):
            self.platform = _safe_label(kwargs["platform"], self.platform)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs: Any,
    ) -> None:
        del kwargs
        self._set_session(new_session_id)
        self.session_lineage = {
            "parent_session_hash": _session_hash(parent_session_id) if parent_session_id else "",
            "reset": bool(reset),
            "rewound": bool(rewound),
        }
        self.turn_id = ""
        self._cache.clear()

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        del messages
        self._queue_improve()
        self._wake.set()
        self.drain_once()

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        del messages
        self._wake.set()
        self.drain_once()
        return ""

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self.agent_context != "primary":
            return
        if action in {"add", "replace"}:
            self._queue_text(content, origin=f"builtin-{target}-{action}")
            return
        if action == "remove":
            values = metadata or {}
            self._queue_forget_preview(
                claim_id=_positive_id(values.get("claim_id")),
                source_item_id=_positive_id(values.get("source_item_id")),
            )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        key = self._cache_key(query, session_id)
        cached = self._cache.get(key)
        if cached and cached[0] > self.clock():
            return cached[1]
        self.queue_prefetch(query, session_id=session_id)
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not query.strip() or (self._prefetch_thread and self._prefetch_thread.is_alive()):
            return
        self._prefetch_thread = threading.Thread(
            target=self._run_prefetch,
            args=(query, session_id),
            name="memorymaster-prefetch",
            daemon=True,
        )
        self._prefetch_thread.start()

    def recall_now(self, query: str) -> str:
        assert self.backend is not None
        try:
            return self.backend.recall(
                query, scope=self.scope_value, session_id=self.session_hash
            )
        except BackendTransientError:
            if self.replica_backend is None:
                return ""
            return str(
                self.replica_backend.recall(
                    query, scope=self.scope_value, session_id=self.session_hash
                )
            )

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [_recall_schema(), _remember_schema(), _scope_schema(), _forget_schema()]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        del kwargs
        try:
            result = self._dispatch_tool(tool_name, args)
            return json.dumps(result, sort_keys=True)
        except (
            BackendAuthError,
            BackendPayloadError,
            BackendScopeError,
            BackendTransientError,
        ) as exc:
            return json.dumps({"ok": False, "error": exc.code}, sort_keys=True)
        except (KeyError, TypeError, ValueError) as exc:
            return json.dumps({"ok": False, "error": type(exc).__name__}, sort_keys=True)

    def drain_once(self) -> bool:
        if self.outbox is None or self.backend is None:
            return False
        if self.clock() < self._circuit_open_until:
            return False
        entry = self.outbox.lease_next()
        if entry is None:
            return False
        self._deliver(entry)
        return True

    def status(self) -> dict[str, Any]:
        counts = self.outbox.counts() if self.outbox else {}
        return {
            **counts,
            "provider": self.name,
            "scope": self.scope_value,
            "circuit_open": self.clock() < self._circuit_open_until,
        }

    def shutdown(self) -> None:
        if self.config is None:
            return
        deadline = time.monotonic() + self.config.shutdown_drain_seconds
        self._stop.set()
        self._wake.set()
        if self._worker:
            self._worker.join(timeout=max(0.0, deadline - time.monotonic()))
        drainer = threading.Thread(target=self.drain_once, daemon=True)
        drainer.start()
        drainer.join(timeout=max(0.0, deadline - time.monotonic()))
        if not drainer.is_alive():
            self.close_outbox()

    def backup_paths(self) -> list[str]:
        if self.config:
            return [str(self.config.outbox_path)]
        home = Path.home() / ".hermes"
        return [str((home / "memorymaster-outbox.db").resolve())]

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "endpoint",
                "description": "Authenticated MemoryMaster streamable-MCP URL",
                "required": True,
                "env_var": "MEMORYMASTER_HERMES_MCP_URL",
            },
            {
                "key": "token",
                "description": "MemoryMaster MCP bearer token",
                "secret": True,
                "required": True,
                "env_var": "MEMORYMASTER_HERMES_MCP_TOKEN",
            },
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        path = Path(hermes_home).expanduser().resolve() / CONFIG_NAME
        current = ProviderConfig.load(hermes_home).public_config() if path.exists() else {}
        allowed = {key: value for key, value in values.items() if key != "token"}
        path.write_text(json.dumps({**current, **allowed}, indent=2, sort_keys=True), encoding="utf-8")

    def close_outbox(self) -> None:
        if self.outbox is not None:
            self.outbox.close()
            self.outbox = None

    def _queue_text(self, text: str, *, origin: str) -> dict[str, Any]:
        sanitized, findings = sanitize_outbox_text(text)
        content_hash = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()
        turn_id = self.turn_id or content_hash[:16]
        external_id = f"hermes:{self.session_hash}:{turn_id}"
        metadata = {
            "agent_context": self.agent_context,
            "origin": _safe_label(origin, "turn"),
            "platform": self.platform,
            "redacted": bool(findings),
        }
        if self.session_lineage:
            metadata["session_lineage"] = self.session_lineage
        envelope = _remember_envelope(
            text=sanitized,
            scope=self.scope_value,
            source_agent=self.source_agent,
            session_hash=self.session_hash,
            turn_id=turn_id,
            external_id=external_id,
            content_hash=content_hash,
            metadata=metadata,
        )
        return self._enqueue(envelope)

    def _queue_forget_preview(self, *, claim_id: int = 0, source_item_id: int = 0) -> dict[str, Any]:
        if not claim_id and not source_item_id:
            return {"ok": False, "error": "missing_forget_target"}
        payload = {"claim_id": claim_id, "source_item_id": source_item_id}
        key_material = json.dumps(payload, sort_keys=True)
        envelope = {
            "contract": CONTRACT,
            "operation": "forget_preview",
            "identity": {
                "session_hash": self.session_hash,
                "source_agent": self.source_agent,
                "turn_id": self.turn_id or "memory-write",
                "content_hash": hashlib.sha256(key_material.encode()).hexdigest(),
                "external_id": f"forget:{claim_id}:{source_item_id}",
            },
            "payload": payload,
        }
        return self._enqueue(envelope)

    def _queue_improve(self) -> dict[str, Any]:
        payload = {"scope": self.scope_value, "max_items": 200}
        identity = f"{self.session_hash}:{self.scope_value}"
        envelope = {
            "contract": CONTRACT,
            "operation": "improve",
            "identity": {
                "session_hash": self.session_hash,
                "source_agent": self.source_agent,
                "turn_id": "session-end",
                "content_hash": hashlib.sha256(identity.encode()).hexdigest(),
                "external_id": f"improve:{identity}",
            },
            "payload": payload,
        }
        return self._enqueue(envelope)

    def _enqueue(self, envelope: dict[str, Any]) -> dict[str, Any]:
        if self.outbox is None:
            raise RuntimeError("provider_not_initialized")
        identity = envelope["identity"]
        replay_material = ":".join(
            (envelope["operation"], identity["external_id"], identity["content_hash"])
        )
        replay_key = hashlib.sha256(replay_material.encode()).hexdigest()
        entry, created = self.outbox.enqueue(replay_key, envelope)
        self._wake.set()
        return {"ok": True, "queued": created, "outbox_id": entry.id}

    def _deliver(self, entry: OutboxEntry) -> None:
        assert self.backend is not None and self.outbox is not None and self.config is not None
        try:
            if entry.envelope["operation"] == "remember":
                self.backend.remember(entry.envelope)
            elif entry.envelope["operation"] == "forget_preview":
                self.backend.forget_preview(**entry.envelope["payload"])
            elif entry.envelope["operation"] == "improve":
                self.backend.improve(**entry.envelope["payload"])
            else:
                self.outbox.block(entry.id, "unsupported_operation")
                return
        except (BackendAuthError, BackendPayloadError, BackendScopeError) as exc:
            self.outbox.block(entry.id, exc.code)
            return
        except BackendTransientError as exc:
            self._retry_or_block(entry, exc.code)
            return
        self._failure_count = 0
        self.outbox.complete(entry.id)

    def _retry_or_block(self, entry: OutboxEntry, code: str) -> None:
        assert self.outbox is not None and self.config is not None
        self._failure_count += 1
        if entry.attempts >= self.config.max_attempts:
            self.outbox.block(entry.id, "attempts_exhausted")
            return
        base = min(
            self.config.retry_cap_seconds,
            self.config.retry_base_seconds * (2 ** max(0, entry.attempts - 1)),
        )
        self.outbox.retry(entry.id, error_code=code, delay_seconds=base * random.uniform(0.8, 1.2))
        if self._failure_count >= self.config.circuit_failure_threshold:
            self._circuit_open_until = self.clock() + self.config.circuit_reset_seconds

    def _dispatch_tool(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        assert self.backend is not None
        if tool_name == "memorymaster_recall":
            return {"ok": True, "context": self.recall_now(str(args["query"]))}
        if tool_name == "memorymaster_remember":
            return self._queue_text(str(args["text"]), origin="tool")
        if tool_name == "memorymaster_forget_preview":
            return self.backend.forget_preview(
                claim_id=_positive_id(args.get("claim_id")),
                source_item_id=_positive_id(args.get("source_item_id")),
            )
        if tool_name == "memorymaster_scope":
            return self._scope_tool(args)
        raise KeyError(tool_name)

    def _scope_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        assert self.backend is not None
        action = str(args.get("action", "show"))
        scope = str(args.get("scope", "")).strip()
        if scope == "global":
            raise BackendScopeError("global_scope_forbidden")
        result = self.backend.scope(
            action,
            session_id=self.session_hash,
            source_agent=self.source_agent,
            platform=self.platform,
            scope=scope,
            task_label=_safe_label(args.get("task_label"), ""),
        )
        if action == "bind" and scope:
            self.scope_value = scope
        elif action == "clear":
            self.scope_value = self.config.default_scope if self.config else "user"
        return result

    def _set_session(self, session_id: str) -> None:
        self.session_hash = _session_hash(session_id)

    def _start_worker(self) -> None:
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="memorymaster-outbox",
            daemon=True,
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            progressed = self.drain_once()
            if not progressed:
                self._wake.wait(timeout=1.0)
                self._wake.clear()

    def _cache_key(self, query: str, session_id: str) -> str:
        session = _session_hash(session_id) if session_id else self.session_hash
        return hashlib.sha256(f"{session}:{self.scope_value}:{query}".encode()).hexdigest()

    def _run_prefetch(self, query: str, session_id: str) -> None:
        context = self.recall_now(query)
        if context and self.config:
            self._cache[self._cache_key(query, session_id)] = (
                self.clock() + self.config.recall_cache_seconds,
                context,
            )


def _session_hash(session_id: str) -> str:
    return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()


def _safe_label(value: Any, default: str) -> str:
    cleaned = _SAFE_LABEL.sub("-", str(value or "").strip()).strip("-:.")
    return cleaned[:80] or default


def _positive_id(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _remember_envelope(**values: Any) -> dict[str, Any]:
    return {
        "contract": CONTRACT,
        "operation": "remember",
        "identity": {
            key: values[key]
            for key in ("external_id", "session_hash", "turn_id", "content_hash", "source_agent")
        },
        "payload": {
            "text": values["text"],
            "scope": values["scope"],
            "source_uri": "",
            "metadata": values["metadata"],
        },
    }


def _recall_schema() -> dict[str, Any]:
    return {
        "name": "memorymaster_recall",
        "description": "Recall confirmed governed memory in the active session scope.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }


def _remember_schema() -> dict[str, Any]:
    return {
        "name": "memorymaster_remember",
        "description": "Queue evidence for governed candidate extraction; never confirms it directly.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }


def _scope_schema() -> dict[str, Any]:
    return {
        "name": "memorymaster_scope",
        "description": "Show, bind, or clear the current session scope. Global scope is forbidden.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["show", "bind", "clear"]},
                "scope": {"type": "string"},
                "task_label": {"type": "string"},
            },
            "required": ["action"],
        },
    }


def _forget_schema() -> dict[str, Any]:
    return {
        "name": "memorymaster_forget_preview",
        "description": "Preview logical forgetting without applying it.",
        "parameters": {
            "type": "object",
            "properties": {
                "claim_id": {"type": "integer", "minimum": 1},
                "source_item_id": {"type": "integer", "minimum": 1},
            },
        },
    }
