"""Authenticated streamable-HTTP entrypoint for MemoryMaster MCP."""

from __future__ import annotations

import argparse
import hmac
import os
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse
from starlette.routing import Route

from memorymaster.surfaces.mcp_server import FastMCP, _read_service, mcp


TOKEN_ENV = "MEMORYMASTER_MCP_HTTP_TOKEN"
ALLOWED_HOSTS_ENV = "MEMORYMASTER_MCP_HTTP_ALLOWED_HOSTS"
DEFAULT_ALLOWED_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")


class BearerAuthMiddleware:
    """Protect MCP traffic without blocking unauthenticated health probes."""

    def __init__(self, app: Any, *, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path") not in {"/healthz", "/readyz"}:
            supplied = self._bearer_token(scope.get("headers", []))
            if supplied is None or not hmac.compare_digest(supplied, self.token):
                response = JSONResponse(
                    {"status": "fail", "error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

    @staticmethod
    def _bearer_token(headers: list[tuple[bytes, bytes]]) -> str | None:
        for name, value in headers:
            if name.lower() != b"authorization":
                continue
            scheme, separator, token = value.decode("latin-1").partition(" ")
            if separator and scheme.lower() == "bearer" and token.strip():
                return token.strip()
        return None


def _required_token(explicit: str | None = None) -> str:
    token = (explicit if explicit is not None else os.environ.get(TOKEN_ENV, "")).strip()
    if not token:
        raise RuntimeError(f"{TOKEN_ENV} is required for the streamable-HTTP MCP service")
    return token


def _allowed_hosts(explicit: list[str] | tuple[str, ...] | None = None) -> list[str]:
    if explicit is not None:
        values = [str(value).strip() for value in explicit]
    else:
        configured = os.environ.get(ALLOWED_HOSTS_ENV, "")
        values = configured.split(",") if configured else list(DEFAULT_ALLOWED_HOSTS)
    resolved = [value for value in values if value]
    if not resolved:
        raise RuntimeError(f"{ALLOWED_HOSTS_ENV} must contain at least one host pattern")
    return resolved


def _db_readiness(db_target: str, workspace: str) -> dict[str, str]:
    try:
        service = _read_service(db_target, workspace)
        with service.store.connect() as connection:
            connection.execute("SELECT 1")
    except Exception as exc:
        return {"status": "fail", "error": str(exc)}
    return {"status": "ok"}


def create_http_app(
    *,
    token: str | None = None,
    db_target: str | Path | None = None,
    workspace: str | Path | None = None,
    allowed_hosts: list[str] | tuple[str, ...] | None = None,
) -> Any:
    """Build the authenticated MCP ASGI app with liveness/readiness routes."""
    if FastMCP is None:  # pragma: no cover - optional dependency guard
        raise RuntimeError("MCP support is not installed. Install with: pip install 'memorymaster[mcp]'")
    if not hasattr(mcp, "streamable_http_app"):  # pragma: no cover - old SDK guard
        raise RuntimeError("The installed MCP SDK lacks streamable HTTP support; install 'mcp>=1.8.1'")
    resolved_token = _required_token(token)
    resolved_db = str(db_target or os.environ.get("MEMORYMASTER_DEFAULT_DB", "memorymaster.db"))
    resolved_workspace = str(workspace or os.environ.get("MEMORYMASTER_WORKSPACE", "."))

    async def healthz(_request: Any) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "memorymaster-mcp-http"})

    async def readyz(_request: Any) -> JSONResponse:
        db_check = _db_readiness(resolved_db, resolved_workspace)
        ready = db_check["status"] == "ok"
        return JSONResponse(
            {"status": "ok" if ready else "fail", "checks": {"db": db_check}},
            status_code=200 if ready else 503,
        )

    mcp.settings.streamable_http_path = "/mcp"
    mcp.settings.json_response = True
    mcp.settings.stateless_http = True
    mcp.settings.transport_security.allowed_hosts = _allowed_hosts(allowed_hosts)
    app = mcp.streamable_http_app()
    app.routes.insert(0, Route("/readyz", readyz, methods=["GET"]))
    app.routes.insert(0, Route("/healthz", healthz, methods=["GET"]))
    app.add_middleware(BearerAuthMiddleware, token=resolved_token)
    return app


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run authenticated MemoryMaster streamable-HTTP MCP")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8765, help="Bind port")
    parser.add_argument("--db", default=os.environ.get("MEMORYMASTER_DEFAULT_DB", "memorymaster.db"))
    parser.add_argument("--workspace", default=os.environ.get("MEMORYMASTER_WORKSPACE", "."))
    parser.add_argument("--allowed-host", action="append", dest="allowed_hosts")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    app = create_http_app(
        db_target=args.db,
        workspace=args.workspace,
        allowed_hosts=args.allowed_hosts,
    )
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
