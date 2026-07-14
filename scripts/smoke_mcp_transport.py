"""Protocol-level MCP initialize/tools-list smoke for stdio or HTTP."""

from __future__ import annotations

import argparse
import asyncio
import os

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


async def _assert_session(read_stream, write_stream) -> None:
    async with ClientSession(read_stream, write_stream) as session:
        initialized = await session.initialize()
        server_info = getattr(initialized, "serverInfo", None)
        server_name = getattr(server_info, "name", "")
        if server_name != "memorymaster":
            raise RuntimeError(f"unexpected MCP server: {server_name}")
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        if "query_memory" not in names:
            raise RuntimeError("MemoryMaster MCP handshake omitted query_memory")


async def _smoke_stdio() -> None:
    parameters = StdioServerParameters(
        command="memorymaster-mcp",
        env={**os.environ, "MEMORYMASTER_MCP_AUTH_MODE": "local-trusted"},
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        await _assert_session(read_stream, write_stream)


async def _smoke_http(url: str, token: str) -> None:
    if not token:
        raise RuntimeError("--token or MEMORYMASTER_MCP_HTTP_TOKEN is required for HTTP smoke")
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as client:
        async with streamable_http_client(url, http_client=client) as (read_stream, write_stream, _):
            await _assert_session(read_stream, write_stream)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", required=True, choices=("stdio", "http"))
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--token", default=os.environ.get("MEMORYMASTER_MCP_HTTP_TOKEN", ""))
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.transport == "stdio":
        asyncio.run(_smoke_stdio())
    else:
        asyncio.run(_smoke_http(args.url, args.token))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
