"""Authenticated MCP transport and strict read-only replica recall backend."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol


class BackendError(RuntimeError):
    def __init__(self, code: str, *, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class BackendTransientError(BackendError):
    """Retryable authority/network failure."""


class BackendAuthError(BackendError):
    """Permanent authentication failure."""


class BackendScopeError(BackendError):
    """Permanent authorization/scope failure."""


class MemoryMasterBackend(Protocol):
    def remember(self, envelope: dict[str, Any]) -> dict[str, Any]: ...

    def recall(self, query: str, *, scope: str, session_id: str) -> str: ...

    def scope(
        self,
        action: str,
        *,
        session_id: str,
        source_agent: str,
        platform: str,
        scope: str = "",
        task_label: str = "",
    ) -> dict[str, Any]: ...

    def forget_preview(
        self, *, claim_id: int = 0, source_item_id: int = 0
    ) -> dict[str, Any]: ...

    def improve(self, *, scope: str, max_items: int = 200) -> dict[str, Any]: ...


class MCPHttpBackend:
    def __init__(self, endpoint: str, token: str, *, timeout_seconds: float = 0.35) -> None:
        self.endpoint = endpoint
        self.token = token
        self.timeout_seconds = timeout_seconds

    def remember(self, envelope: dict[str, Any]) -> dict[str, Any]:
        payload = envelope["payload"]
        identity = envelope["identity"]
        metadata = payload.get("metadata", {})
        return self._call(
            "remember",
            {
                "text": payload["text"],
                "source_uri": payload.get("source_uri", ""),
                "scope": payload["scope"],
                "source_agent": identity["source_agent"],
                "session_id": identity["session_hash"],
                "platform": metadata.get("platform", "hermes"),
                "producer": "hermes",
                "producer_external_id": identity["external_id"],
                "producer_content_hash": identity["content_hash"],
                "producer_session_hash": identity["session_hash"],
                "producer_turn_id": identity["turn_id"],
                "producer_metadata_json": json.dumps(metadata, sort_keys=True),
            },
        )

    def recall(self, query: str, *, scope: str, session_id: str) -> str:
        result = self._call(
            "recall",
            {
                "query": query,
                "scope_allowlist": scope,
                "session_id": session_id,
                "source_agent": "hermes-memorymaster",
                "platform": "hermes",
            },
        )
        return str(result.get("output", ""))

    def scope(
        self,
        action: str,
        *,
        session_id: str,
        source_agent: str,
        platform: str,
        scope: str = "",
        task_label: str = "",
    ) -> dict[str, Any]:
        tool = {"show": "session_scope_show", "bind": "session_scope_bind", "clear": "session_scope_clear"}.get(action)
        if tool is None:
            raise BackendScopeError("invalid_scope_action")
        arguments = {"session_id": session_id, "source_agent": source_agent}
        if action == "bind":
            arguments.update({"scope": scope, "platform": platform, "task_label": task_label})
        elif action == "clear":
            arguments["platform"] = platform
        return self._call(tool, arguments)

    def forget_preview(self, *, claim_id: int = 0, source_item_id: int = 0) -> dict[str, Any]:
        return self._call(
            "forget_preview",
            {"claim_id": claim_id, "source_item_id": source_item_id},
        )

    def improve(self, *, scope: str, max_items: int = 200) -> dict[str, Any]:
        return self._call("improve", {"scope": scope, "max_items": max_items})

    def _call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        server_arguments = {"db": "", "workspace": "", **arguments}
        try:
            return asyncio.run(self._call_async(tool_name, server_arguments))
        except (BackendAuthError, BackendScopeError, BackendTransientError):
            raise
        except Exception as exc:
            raise _classify_transport_error(exc) from exc

    async def _call_async(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        timeout = httpx.Timeout(self.timeout_seconds)
        headers = {"Authorization": f"Bearer {self.token}"}
        async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
            async with streamable_http_client(self.endpoint, http_client=client) as streams:
                async with ClientSession(
                    streams[0], streams[1], read_timeout_seconds=timedelta(seconds=self.timeout_seconds)
                ) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
        return _result_dict(result)


class ReadOnlyReplicaBackend:
    """Recall only; construction deliberately exposes no write method."""

    def __init__(self, db_path: str | Path, workspace: str | Path | None = None) -> None:
        self.db_path = Path(db_path).resolve()
        self.workspace = Path(workspace).resolve() if workspace else self.db_path.parent

    def recall(self, query: str, *, scope: str, session_id: str) -> str:
        from memorymaster.core.service import MemoryService

        service = MemoryService(self.db_path, workspace_root=self.workspace, read_only=True)
        result = service.query_for_context(
            query,
            token_budget=4000,
            output_format="text",
            retrieval_mode="legacy",
            trust_mode="trusted",
            scope_allowlist=[scope],
        )
        return result.output


def _result_dict(result: Any) -> dict[str, Any]:
    if bool(getattr(result, "isError", False)):
        detail = " ".join(str(getattr(item, "text", "")) for item in result.content)
        raise _classify_message(detail)
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for item in getattr(result, "content", []):
        text = getattr(item, "text", "")
        if text:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {"result": parsed}
    return {}


def _classify_message(message: str) -> BackendError:
    lowered = message.lower()
    if "unauthorized" in lowered or "401" in lowered or "token" in lowered:
        return BackendAuthError("unauthorized", detail=message)
    if "scope" in lowered or "permission" in lowered or "forbidden" in lowered or "403" in lowered:
        return BackendScopeError("scope_denied", detail=message)
    return BackendTransientError("authority_error", detail=message)


def _classify_transport_error(exc: Exception) -> BackendError:
    for nested in _walk_exceptions(exc):
        status = getattr(getattr(nested, "response", None), "status_code", None)
        if status == 401 or "401" in str(nested) or "unauthorized" in str(nested).lower():
            return BackendAuthError("unauthorized")
        if status == 403 or "403" in str(nested) or "forbidden" in str(nested).lower():
            return BackendScopeError("scope_denied")
    return BackendTransientError("authority_unavailable")


def _walk_exceptions(exc: BaseException) -> list[BaseException]:
    found: list[BaseException] = []
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        pending.extend(getattr(current, "exceptions", ()))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return found
