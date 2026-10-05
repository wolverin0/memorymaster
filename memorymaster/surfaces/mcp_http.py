"""Authenticated streamable-HTTP entrypoint for MemoryMaster MCP."""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import time
import threading
import sys
from functools import partial
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse
from starlette.routing import Route

from memorymaster.core.structured_log import configure_structured_logging, timed_event
from memorymaster.surfaces.mcp_server import (
    FastMCP,
    _limit_native_threads,
    _preload_native_ml,
    _read_service,
    mcp,
)


TOKEN_ENV = "MEMORYMASTER_MCP_HTTP_TOKEN"
ALLOWED_HOSTS_ENV = "MEMORYMASTER_MCP_HTTP_ALLOWED_HOSTS"
DEFAULT_ALLOWED_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")
HOOK_RECALL_PATH = "/hook/recall"
_HOOK_LOG = logging.getLogger("memorymaster.hook_recall")
HOOK_RECALL_MAX_BYTES = 256 * 1024
# T-0594: the UserPromptSubmit hook used to recall from a cold process on every
# prompt: ~1.5 s of a ~2.1 s hook was opening the 7.6 GB DB and building caches,
# while the same recall in a warm process takes ~0.05 s. Only the loopback,
# local-trusted shared server serves it; a team-mode server (Hermes) does not.
_HOOK_RECALL_THREADS = 4
_hook_recall_limiter: Any = None


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

    async def hook_recall(request: Any) -> JSONResponse:
        global _hook_recall_limiter
        body = await request.body()
        if len(body) > HOOK_RECALL_MAX_BYTES:
            return JSONResponse({"error": "payload_too_large"}, status_code=413)
        try:
            payload = json.loads(body or b"{}")
            query = str(payload.get("query") or "")
            hook_data = payload.get("hook_data")
            if hook_data is not None and not isinstance(hook_data, dict):
                raise ValueError("hook_data must be an object")
        except (ValueError, AttributeError):
            return JSONResponse({"error": "bad_request"}, status_code=400)
        import anyio
        from memorymaster.context_hook import recall

        if _hook_recall_limiter is None:
            _hook_recall_limiter = anyio.CapacityLimiter(_HOOK_RECALL_THREADS)
        extra = {"hook_data": hook_data} if hook_data else {}
        with timed_event(_HOOK_LOG, "hook_recall", surface="hook", query_chars=len(query)) as fields:
            ctx = await anyio.to_thread.run_sync(
                partial(recall, query, db_path=resolved_db, skip_qdrant=True, **extra),
                limiter=_hook_recall_limiter,
            )
            fields["ctx_chars"] = len(ctx or "")
        return JSONResponse({"ctx": ctx or ""})

    mcp.settings.streamable_http_path = "/mcp"
    mcp.settings.json_response = True
    mcp.settings.stateless_http = True
    mcp.settings.transport_security.allowed_hosts = _allowed_hosts(allowed_hosts)
    app = mcp.streamable_http_app()
    app.routes.insert(0, Route("/readyz", readyz, methods=["GET"]))
    app.routes.insert(0, Route("/healthz", healthz, methods=["GET"]))
    if os.environ.get("MEMORYMASTER_MCP_AUTH_MODE", "").strip().lower() == "local-trusted":
        app.routes.insert(0, Route(HOOK_RECALL_PATH, hook_recall, methods=["POST"]))
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
    _limit_native_threads()
    # JSON lines from the first log record on: one parseable stream for the server log (T-0764).
    os.environ.setdefault("TQDM_DISABLE", "1")  # model progress bars would write raw text to stderr
    configure_structured_logging(sys.stderr, component="mcp-http", exclusive=True)
    _preload_native_ml()  # a lazy torch import inside the event loop stalls on Windows (T-0726 POC)
    args = _build_parser().parse_args(argv)
    app = create_http_app(
        db_target=args.db,
        workspace=args.workspace,
        allowed_hosts=args.allowed_hosts,
    )
    _start_tokenizer_warmer(str(args.db or os.environ.get("MEMORYMASTER_DEFAULT_DB", "memorymaster.db")))
    _serve(app, host=args.host, port=args.port)
    return 0


TOKENIZER_WARM_ENV = "MEMORYMASTER_TOKENIZER_WARM_SECONDS"


def _warm_tokenizer_once(db_path: str, last: int | None) -> int | None:
    """Build the prompt-recall token statistics now if the text generation moved.

    Profiled 2026-10-05: a fresh process' first recall took 1,426 ms, 1,216 of them
    building these statistics and the alias set; the second took 65 ms. Building
    them here, off the request path, means no prompt pays for it after a boot or a
    new claim. Returns the generation it warmed (unchanged if nothing moved).
    """
    from memorymaster.recall import recall_tokenizer

    generation = recall_tokenizer.read_text_generation(db_path)
    if generation == last:
        return last
    recall_tokenizer.extract_query_tokens("memorymaster recall warmup", db_path)
    return generation


def _start_tokenizer_warmer(db_path: str) -> threading.Thread | None:
    """Warm at boot, then re-warm whenever the text generation moves (0 disables)."""
    try:
        interval = float(os.environ.get(TOKENIZER_WARM_ENV, "30"))
    except ValueError:
        interval = 30.0
    if interval <= 0:
        return None

    def run() -> None:
        last: int | None = None
        while True:
            try:
                last = _warm_tokenizer_once(db_path, last)
            except Exception:  # noqa: BLE001 - warming is an optimisation; recall still works cold
                _HOOK_LOG.debug("tokenizer warmup failed", exc_info=True)
            time.sleep(interval)

    thread = threading.Thread(target=run, name="tokenizer-warmer", daemon=True)
    thread.start()
    return thread


def _serve(app: Any, *, host: str, port: int) -> None:
    import asyncio

    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, loop="none", log_config=None))

    async def serve_with_stall_dump() -> None:
        watchdog = asyncio.create_task(_loop_stall_dump(_stall_dump_seconds()))
        try:
            await server.serve()
        finally:
            watchdog.cancel()
            _cancel_stall_dump()

    if os.name != "nt":
        asyncio.run(serve_with_stall_dump())  # uvicorn logs reach the JSON root handler
        return
    # Windows: uvicorn's default ProactorEventLoop closes the LISTENING socket when an
    # accept completes with an error (a client reset before accept, WinError 64), leaving
    # a live process that serves nothing and never restarts. The selector loop does not.
    asyncio.run(serve_with_stall_dump(), loop_factory=asyncio.SelectorEventLoop)


STALL_DUMP_ENV = "MEMORYMASTER_MCP_STALL_DUMP_SECONDS"
_STALL_REARM_SECONDS = 5.0


def _stall_dump_seconds() -> float:
    """Seconds the event loop may stop before every thread's stack is dumped (0 disables).

    The shared server froze several times a day and the supervisor killed it after
    three failed health probes (~35 s), leaving no trace of what blocked the loop.
    15 s dumps first.
    """
    try:
        return max(0.0, float(os.environ.get(STALL_DUMP_ENV, "15")))
    except ValueError:
        return 15.0


async def _loop_stall_dump(seconds: float) -> None:
    """Re-arm faulthandler's timer from the loop; if the loop stops, the C timer thread dumps.

    faulthandler's watchdog is a C thread that does not need the GIL, so the dump
    happens even when a worker holds it. Each re-arm cancels the previous timer.
    """
    if seconds <= 0:
        return
    import asyncio
    import faulthandler

    stream = sys.stderr
    if not hasattr(stream, "fileno"):
        return
    try:
        stream.fileno()
    except (OSError, ValueError):
        return
    while True:
        faulthandler.dump_traceback_later(seconds, repeat=False, file=stream, exit=False)
        await asyncio.sleep(_STALL_REARM_SECONDS)


def _cancel_stall_dump() -> None:
    import faulthandler

    faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    raise SystemExit(main())
