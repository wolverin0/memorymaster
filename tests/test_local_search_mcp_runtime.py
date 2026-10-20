from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from memorymaster.bridges.local_search.provider import PathHit
from memorymaster.core.service import MemoryService
from memorymaster.surfaces import mcp_server
from memorymaster.surfaces.cli import build_parser
from memorymaster.surfaces.mcp_usage import query_window


class RecordingProvider:
    def __init__(self, path: str) -> None:
        self.path = path
        self.calls: list[dict] = []

    def available(self) -> bool:
        return True

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        kind: str = "any",
        whole_name: bool = False,
    ) -> list[PathHit]:
        self.calls.append(
            {"query": query, "limit": limit, "kind": kind, "whole_name": whole_name}
        )
        return [PathHit(path=self.path, kind=kind, size=None, modified=None)]


@pytest.fixture(autouse=True)
def local_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")


def _initialized_db(tmp_path) -> str:
    db_path = tmp_path / "usage.db"
    svc = MemoryService(db_path, workspace_root=tmp_path)
    svc.init_db()
    return str(db_path)


def test_cli_exposes_exact_search_and_opt_in_remember() -> None:
    parser = build_parser()

    search_args = parser.parse_args(["local-search", "AGENTS.md", "--exact"])
    resolve_args = parser.parse_args(["resolve-project", "memorymaster", "--remember"])

    assert search_args.exact is True
    assert resolve_args.remember is True


def test_mcp_local_search_exposes_exact_mode(monkeypatch, tmp_path) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text("rules", encoding="utf-8")
    provider = RecordingProvider(str(target))
    monkeypatch.setattr(
        "memorymaster.bridges.local_search.everything.EverythingProvider",
        lambda: provider,
    )
    monkeypatch.setattr(
        "memorymaster.bridges.local_search.redact.load_roots",
        lambda: [("workspace", str(tmp_path))],
    )

    payload = mcp_server.local_search(
        "AGENTS.md",
        kind="file",
        exact=True,
        db=_initialized_db(tmp_path),
        workspace=str(tmp_path),
    )

    assert payload["ok"] is True
    assert payload["exact"] is True
    assert provider.calls == [
        {"query": "AGENTS.md", "limit": 50, "kind": "file", "whole_name": True}
    ]


def test_mcp_local_search_persists_privacy_safe_usage(monkeypatch, tmp_path) -> None:
    target = tmp_path / "secret-query-result.txt"
    target.write_text("x", encoding="utf-8")
    provider = RecordingProvider(str(target))
    monkeypatch.setattr(
        "memorymaster.bridges.local_search.everything.EverythingProvider",
        lambda: provider,
    )
    monkeypatch.setattr(
        "memorymaster.bridges.local_search.redact.load_roots",
        lambda: [("workspace", str(tmp_path))],
    )
    db_path = _initialized_db(tmp_path)

    mcp_server.local_search(
        "do-not-store-this-query",
        db=db_path,
        workspace=str(tmp_path),
    )

    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    rows = query_window(db_path, since)
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "local_search:fuzzy"
    assert rows[0]["result_status"] == "ok_hits"
    assert "do-not-store-this-query" not in json.dumps(rows)
    assert "secret-query-result.txt" not in json.dumps(rows)


def test_mcp_local_search_redacts_paths_outside_registered_roots(
    monkeypatch, tmp_path
) -> None:
    registered = tmp_path / "registered"
    registered.mkdir()
    unregistered = tmp_path / "private-user" / "secret-project"
    unregistered.mkdir(parents=True)
    target = unregistered / "AGENTS.md"
    target.write_text("rules", encoding="utf-8")
    provider = RecordingProvider(str(target))
    monkeypatch.setattr(
        "memorymaster.bridges.local_search.everything.EverythingProvider",
        lambda: provider,
    )
    monkeypatch.setattr(
        "memorymaster.bridges.local_search.redact.load_roots",
        lambda: [("workspace", str(registered))],
    )

    payload = mcp_server.local_search(
        "AGENTS.md",
        exact=True,
        db=_initialized_db(tmp_path),
        workspace=str(registered),
    )

    displayed = payload["hits"][0]["path"]
    assert displayed.startswith("unregistered/")
    assert displayed.endswith("/AGENTS.md")
    assert str(target) not in displayed
    assert "private-user" not in displayed


def test_mcp_resolve_project_is_read_only_unless_remembered(
    monkeypatch, tmp_path
) -> None:
    project = tmp_path / "memorymaster"
    project.mkdir()
    (project / ".git").mkdir()
    (project / "AGENTS.md").write_text("rules", encoding="utf-8")
    provider = RecordingProvider(str(project))
    monkeypatch.setattr(
        "memorymaster.bridges.local_search.everything.EverythingProvider",
        lambda: provider,
    )
    monkeypatch.setattr(
        "memorymaster.bridges.local_search.redact.load_roots",
        lambda: [("workspace", str(tmp_path))],
    )
    db_path = _initialized_db(tmp_path)

    payload = mcp_server.resolve_project(
        "memorymaster",
        remember=False,
        db=db_path,
        workspace=str(tmp_path),
    )

    svc = MemoryService(db_path, workspace_root=tmp_path)
    claims = svc.store.list_claims(limit=100, scope_allowlist=["project:memorymaster"])
    rows = query_window(db_path, datetime.now(timezone.utc) - timedelta(minutes=1))
    assert payload["remembered"] is False
    assert [claim for claim in claims if claim.predicate == "local_path"] == []
    assert rows[-1]["tool_name"] == "resolve_project"
    assert rows[-1]["result_status"] == "everything_match"


# --------------------------------------------------------------------------
# T-1645: bounded content search through the MCP tool surface
# --------------------------------------------------------------------------


def _content_env(monkeypatch, tmp_path, provider):
    monkeypatch.setattr(
        "memorymaster.bridges.local_search.everything.EverythingProvider",
        lambda: provider,
    )
    monkeypatch.setattr(
        "memorymaster.bridges.local_search.redact.load_roots",
        lambda: [("workspace", str(tmp_path))],
    )
    monkeypatch.setattr("memorymaster.bridges.local_search.content.rg_binary", lambda: None)


def test_mcp_local_search_without_content_query_is_unchanged(monkeypatch, tmp_path) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text("needle", encoding="utf-8")
    provider = RecordingProvider(str(target))
    _content_env(monkeypatch, tmp_path, provider)

    for blank in ({}, {"content_query": ""}, {"content_query": "   "}):
        provider.calls.clear()
        payload = mcp_server.local_search(
            "AGENTS.md", kind="file", db=_initialized_db(tmp_path), workspace=str(tmp_path), **blank
        )
        assert set(payload) == {"ok", "hits", "degraded", "exact"}
        assert payload["hits"] == [
            {"path": "workspace/AGENTS.md", "kind": "file", "size": None, "modified": None}
        ]
        assert provider.calls == [
            {"query": "AGENTS.md", "limit": 50, "kind": "file", "whole_name": False}
        ]


def test_mcp_local_search_content_query_returns_redacted_hits(monkeypatch, tmp_path) -> None:
    (tmp_path / "docs").mkdir()
    target = tmp_path / "docs" / "notes.md"
    secret = "sk-ant-" + "Q1w2E3r4T5y6U7i8O9p0A1"
    target.write_text(f"intro\nthe needle is here {secret}\n", encoding="utf-8")
    provider = RecordingProvider(str(target))
    _content_env(monkeypatch, tmp_path, provider)

    payload = mcp_server.local_search(
        "*",
        content_query="needle",
        extension="md",
        db=_initialized_db(tmp_path),
        workspace=str(tmp_path),
    )

    assert payload["ok"] is True
    assert payload["content"]["engine"] == "python"
    assert payload["hits"][0]["path"] == "workspace/docs/notes.md"
    assert payload["hits"][0]["line"] == 2
    assert "sk-ant-" not in json.dumps(payload)
    assert str(tmp_path) not in json.dumps(payload)
    assert provider.calls[0]["kind"] == "file"


def test_mcp_local_search_content_usage_never_stores_query_or_paths(monkeypatch, tmp_path) -> None:
    target = tmp_path / "private-notes.txt"
    target.write_text("do-not-store-this-needle\n", encoding="utf-8")
    _content_env(monkeypatch, tmp_path, RecordingProvider(str(target)))
    db_path = _initialized_db(tmp_path)

    mcp_server.local_search(
        "*", content_query="do-not-store-this-needle", db=db_path, workspace=str(tmp_path)
    )

    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    rows = query_window(db_path, since)
    assert [r["tool_name"] for r in rows] == ["local_search:content"]
    assert rows[0]["result_status"] == "ok_hits"
    assert "do-not-store-this-needle" not in json.dumps(rows)
    assert "private-notes" not in json.dumps(rows)


def test_mcp_local_search_content_validation_errors_are_structured(monkeypatch, tmp_path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("needle", encoding="utf-8")
    _content_env(monkeypatch, tmp_path, RecordingProvider(str(target)))
    db_path = _initialized_db(tmp_path)

    bad_ext = mcp_server.local_search(
        "*", content_query="needle", extension="py;../x", db=db_path, workspace=str(tmp_path)
    )
    assert bad_ext["ok"] is False and bad_ext["field"] == "extension"

    outside = tmp_path.parent
    bad_folder = mcp_server.local_search(
        "*", content_query="needle", folder=str(outside), db=db_path, workspace=str(tmp_path)
    )
    assert bad_folder["ok"] is False and bad_folder["field"] == "folder"


def test_mcp_local_search_content_can_be_disabled(monkeypatch, tmp_path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("needle", encoding="utf-8")
    _content_env(monkeypatch, tmp_path, RecordingProvider(str(target)))
    monkeypatch.setenv("MEMORYMASTER_LOCAL_SEARCH_CONTENT", "0")

    payload = mcp_server.local_search(
        "*", content_query="needle", db=_initialized_db(tmp_path), workspace=str(tmp_path)
    )
    assert payload["ok"] is False and payload["code"] == "DISABLED"


def test_mcp_local_search_signature_keeps_existing_parameter_order() -> None:
    import inspect

    names = list(inspect.signature(mcp_server.local_search).parameters)
    assert names[:6] == ["query", "limit", "kind", "exact", "db", "workspace"]
    assert names[6:] == ["content_query", "extension", "folder", "max_hits"]
