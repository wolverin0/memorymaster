from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest


TOKEN = "test-only-http-token"


async def _asgi_request(
    app: Any,
    path: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    sent: list[dict[str, Any]] = []
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    raw_headers = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8765),
        "state": {},
    }
    await app(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    response_headers = {key.decode(): value.decode() for key, value in start.get("headers", [])}
    response_body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    return int(start["status"]), response_headers, response_body


def test_http_entrypoint_requires_a_startup_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from memorymaster.surfaces import mcp_http

    monkeypatch.delenv("MEMORYMASTER_MCP_HTTP_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="MEMORYMASTER_MCP_HTTP_TOKEN"):
        mcp_http.create_http_app(db_target="unused.db")


def test_http_entrypoint_exposes_unauthenticated_liveness_but_protects_mcp(
    tmp_path: Path,
) -> None:
    from memorymaster.surfaces import mcp_http

    app = mcp_http.create_http_app(token=TOKEN, db_target=str(tmp_path / "missing.db"))
    status, _, payload = asyncio.run(_asgi_request(app, "/healthz"))
    assert status == 200
    assert json.loads(payload)["status"] == "ok"

    status, _, payload = asyncio.run(_asgi_request(app, "/mcp", method="POST"))
    assert status == 401
    assert json.loads(payload)["error"] == "unauthorized"

    status, _, _ = asyncio.run(
        _asgi_request(
            app,
            "/mcp",
            method="POST",
            headers={"authorization": "Bearer wrong-token"},
        )
    )
    assert status == 401


def test_http_readiness_reports_missing_database(tmp_path: Path) -> None:
    from memorymaster.surfaces import mcp_http

    app = mcp_http.create_http_app(token=TOKEN, db_target=str(tmp_path / "missing.db"))
    status, _, payload = asyncio.run(_asgi_request(app, "/readyz"))
    assert status == 503
    assert json.loads(payload)["checks"]["db"]["status"] == "fail"


def test_http_mcp_initialize_handshake_accepts_valid_bearer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from memorymaster.core.service import MemoryService
    from memorymaster.surfaces import mcp_http

    db = tmp_path / "ready.db"
    MemoryService(db, workspace_root=tmp_path).init_db()
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    app = mcp_http.create_http_app(token=TOKEN, db_target=str(db), workspace=str(tmp_path))
    request = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "r34-smoke", "version": "1"},
            },
        }
    ).encode()
    async def initialize() -> tuple[int, dict[str, str], bytes]:
        async with app.router.lifespan_context(app):
            return await _asgi_request(
                app,
                "/mcp",
                method="POST",
                headers={
                    "authorization": f"Bearer {TOKEN}",
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                    "host": "127.0.0.1:8765",
                },
                body=request,
            )

    status, _, payload = asyncio.run(initialize())
    assert status == 200
    assert b'"serverInfo"' in payload
