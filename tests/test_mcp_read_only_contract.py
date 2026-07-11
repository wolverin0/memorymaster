"""Red contract for one read-only retrieval per MCP context request (MM-REL-02)."""
from __future__ import annotations

import sqlite3

import pytest

import memorymaster.surfaces.mcp_server as mcp_server
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


@pytest.mark.xfail(
    strict=True,
    reason="audit baseline MM-REL-02: MCP context summaries query and write twice",
)
def test_mcp_context_summary_queries_once_without_writing_access_count(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Formatting detail must reuse one governed read, not run retrieval again."""
    workspace = tmp_path / "readonly"
    workspace.mkdir()
    db = str(tmp_path / "mcp-read.db")
    svc = MemoryService(db, workspace_root=workspace)
    svc.init_db()
    claim = svc.ingest(
        "readonlycontract uses one governed retrieval",
        [CitationInput(source="test://mcp-read")],
        scope="project:readonly",
    )

    calls = 0
    original_query_rows = MemoryService.query_rows

    def counted_query_rows(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original_query_rows(self, *args, **kwargs)

    monkeypatch.setattr(MemoryService, "query_rows", counted_query_rows)
    monkeypatch.setattr(mcp_server, "_ENV_DEFAULT_PROJECT_SCOPE", "")
    monkeypatch.setattr(mcp_server, "_ENV_DEFAULT_WORKSPACE", "")
    monkeypatch.setattr(mcp_server, "_ENV_QUERY_INCLUDE_LEGACY_PROJECT", False)
    monkeypatch.delenv("QDRANT_URL", raising=False)

    result = mcp_server.query_for_context(
        query="readonlycontract",
        db=db,
        workspace=str(workspace),
        retrieval_mode="legacy",
        include_candidates=True,
        scope_allowlist="project:readonly",
        detail_level="summary",
    )
    with sqlite3.connect(db) as conn:
        access_count = int(
            conn.execute(
                "SELECT access_count FROM claims WHERE id = ?", (claim.id,)
            ).fetchone()[0]
        )

    assert result["claims"], "fixture must exercise structured context retrieval"
    assert (calls, access_count) == (1, 0)
