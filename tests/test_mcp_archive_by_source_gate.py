"""G-6: archive_by_source over MCP applies only with the server-side operator opt-in."""
from __future__ import annotations

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService

CIT = [CitationInput(source="test://g6")]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    monkeypatch.delenv("MEMORYMASTER_MCP_OPERATOR_RESOLVE", raising=False)
    svc = MemoryService(tmp_path / "g6.db", workspace_root=tmp_path)
    svc.init_db()
    return svc


def _status(svc, cid):
    return svc.store.get_claim(cid, include_citations=False).status


def test_archive_by_source_apply_needs_the_operator_opt_in(env, monkeypatch):
    from memorymaster.surfaces.mcp_server import archive_by_source

    c = env.ingest("Eval pollution fact about zorblax retention", CIT, scope="project:g6", source_agent="eval-bot")
    kw = dict(source_agent="eval-bot", db=str(env.store.db_path), workspace=str(env.workspace_root))
    dry = archive_by_source(dry_run=True, **kw)
    assert dry["ok"] is True and dry["matched"] == 1
    refused = archive_by_source(dry_run=False, **kw)
    assert refused["ok"] is False and refused["code"] == "OPERATOR_ONLY"
    assert _status(env, c.id) == "candidate"
    monkeypatch.setenv("MEMORYMASTER_MCP_OPERATOR_RESOLVE", "1")
    assert archive_by_source(dry_run=False, **kw)["archived"] == 1
    assert _status(env, c.id) == "archived"
