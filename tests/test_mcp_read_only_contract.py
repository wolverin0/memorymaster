"""Red contract for one read-only retrieval per MCP context request (MM-REL-02)."""
from __future__ import annotations

import json
import sqlite3

import memorymaster.surfaces.mcp_server as mcp_server
from memorymaster.core import spool
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


def test_mcp_context_summary_queries_once_without_writing_access_count(
    tmp_path, monkeypatch
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
    monkeypatch.setenv(spool.ENV_SPOOL_DIR, str(tmp_path / "spool"))

    result = mcp_server.query_for_context(
        query="readonlycontract",
        db=db,
        workspace=str(workspace),
        retrieval_mode="legacy",
        trust_mode="exploratory",
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

    envelopes = [
        json.loads(line)
        for path in spool.spool_dir_for(db).glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert result["claims"], "fixture must exercise structured context retrieval"
    assert (calls, access_count) == (1, 0)
    assert len(envelopes) == 1
    assert envelopes[0]["op"] == "recall"
    assert envelopes[0]["payload"]["claim_ids"] == [claim.id]
