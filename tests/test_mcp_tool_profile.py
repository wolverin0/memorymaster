"""Discovery-only core profile retains Hermes calls and authorization contracts."""

import asyncio
import json
from pathlib import Path
import runpy

import httpx
import pytest

from memorymaster.surfaces.mcp_server import AuthorizedFastMCP, mcp

H = runpy.run_path(str(Path("tests/test_hermes_memory_provider_http.py").resolve()))


def test_profile_default_validation_and_discovery_reduction(monkeypatch):
    monkeypatch.delenv("MEMORYMASTER_MCP_TOOL_PROFILE", raising=False)
    assert AuthorizedFastMCP("fixture").tool_profile == "full"
    monkeypatch.setenv("MEMORYMASTER_MCP_TOOL_PROFILE", "invalid")
    with pytest.raises(ValueError, match="full or core"):
        AuthorizedFastMCP("fixture")
    monkeypatch.setattr(mcp, "tool_profile", "full")
    full = asyncio.run(mcp.list_tools())
    monkeypatch.setattr(mcp, "tool_profile", "core")
    core = asyncio.run(mcp.list_tools())
    assert {tool.name for tool in core} == {"remember", "recall", "forget", "improve"}
    by_name = {tool.name: tool.model_dump() for tool in full}
    assert all(tool.model_dump() == by_name[tool.name] for tool in core)
    assert len(json.dumps([t.model_dump() for t in core]).encode()) <= len(json.dumps([t.model_dump() for t in full]).encode()) / 2


def test_full_and_core_same_hermes_http_journey(tmp_path, monkeypatch):
    metrics = {}
    for profile in ("full", "core"):
        monkeypatch.setenv("MEMORYMASTER_MCP_TOOL_PROFILE", profile)
        case = tmp_path / profile
        case.mkdir()
        generator = H["mcp_http_server"].__wrapped__(case)
        runtime = next(generator)
        endpoint, token, _, _ = runtime
        try:
            response = httpx.post(endpoint, headers={"Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, timeout=5)
            response.raise_for_status()
            result = response.json()["result"]
            metrics[profile] = {"bytes": len(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()),
                                "tools": len(result["tools"])}
            if profile == "core":
                assert {t["name"] for t in result["tools"]} == {"remember", "recall", "forget", "improve"}
            H["test_authenticated_mcp_http_delivers_disposable_capture"](runtime, case, monkeypatch)
            H["test_authoritative_hermes_recall_injects_confirmed_skill"](runtime)
            H["test_mcp_http_rejects_wrong_token_as_permanent_auth_error"](runtime)
            backend = H["MCPHttpBackend"](endpoint, token, timeout_seconds=10, delivery_timeout_seconds=10)
            assert backend.improve(scope="project:workspace")["ok"] is True
            with pytest.raises(Exception, match="cannot perform.*delete"):
                backend._call("forget", {"claim_id": 1, "apply": True})
        finally:
            try:
                next(generator)
            except StopIteration:
                pass
    metrics["reduction"] = 1 - metrics["core"]["bytes"] / metrics["full"]["bytes"]
    assert metrics["reduction"] >= 0.5
    output = Path("artifacts/e2e-fixes-20260907/mcp-discovery.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
