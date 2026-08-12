"""Authenticated MCP transport and strict read-only replica recall backend."""

from __future__ import annotations

import asyncio
import json
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


class BackendPayloadError(BackendError):
    """Permanent payload rejection that must never be retried."""


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
    def __init__(
        self,
        endpoint: str,
        token: str,
        *,
        timeout_seconds: float = 0.35,
        delivery_timeout_seconds: float | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.delivery_timeout_seconds = delivery_timeout_seconds or timeout_seconds

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
            timeout_seconds=self.delivery_timeout_seconds,
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
                "retrieval_mode": "legacy",
                "include_skills": True,
                "skill_limit": 3,
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
        return self._call(
            "improve",
            {"scope": scope, "max_items": max_items},
            timeout_seconds=self.delivery_timeout_seconds,
        )

    def _call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        server_arguments = {"db": "", "workspace": "", **arguments}
        effective_timeout = timeout_seconds or self.timeout_seconds
        try:
            return asyncio.run(
                self._call_async(
                    tool_name,
                    server_arguments,
                    timeout_seconds=effective_timeout,
                )
            )
        except (
            BackendAuthError,
            BackendScopeError,
            BackendPayloadError,
            BackendTransientError,
        ):
            raise
        except Exception as exc:
            raise _classify_transport_error(exc) from exc

    async def _call_async(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        import httpx

        timeout = httpx.Timeout(timeout_seconds)
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json, text/event-stream",
        }
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
            response = await client.post(self.endpoint, json=request)
            response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise BackendTransientError("authority_response_invalid")
        if "error" in payload:
            raise _classify_message(json.dumps(payload["error"], sort_keys=True))
        return _result_dict(payload.get("result", {}))


class ReadOnlyReplicaBackend:
    """Recall only; construction deliberately exposes no write method."""

    def __init__(self, db_path: str | Path, workspace: str | Path | None = None) -> None:
        self.db_path = Path(db_path).resolve()
        self.workspace = Path(workspace).resolve() if workspace else self.db_path.parent

    def recall(self, query: str, *, scope: str, session_id: str) -> str:
        from memorymaster.core.service import MemoryService
        from memorymaster.knowledge.context_bundle import query_context_bundle

        service = MemoryService(self.db_path, workspace_root=self.workspace, read_only=True)
        result = query_context_bundle(
            service,
            query,
            scope_allowlist=[scope],
            token_budget=4000,
            output_format="text",
            retrieval_mode="legacy",
            trust_mode="trusted",
            include_skills=True,
            skill_limit=3,
        )
        return result.output


def _result_dict(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        is_error = bool(result.get("isError", False))
        content = result.get("content", [])
        structured = result.get("structuredContent")
    else:
        is_error = bool(getattr(result, "isError", False))
        content = getattr(result, "content", [])
        structured = getattr(result, "structuredContent", None)
    if is_error:
        detail = " ".join(_content_text(item) for item in content)
        raise _classify_message(detail)
    if isinstance(structured, dict):
        return structured
    for item in content:
        text = _content_text(item)
        if text:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {"result": parsed}
    return {}


def _content_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("text", ""))
    return str(getattr(item, "text", ""))


def _classify_message(message: str) -> BackendError:
    lowered = message.lower()
    if "unsafe persisted data" in lowered:
        return BackendPayloadError("unsafe_persisted_data")
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
