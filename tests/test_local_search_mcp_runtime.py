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
