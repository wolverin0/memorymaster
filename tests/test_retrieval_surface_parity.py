"""Red contracts for governed recall behavior at the MCP surface."""

from __future__ import annotations

import pytest

from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.surfaces.mcp_server import _project_scope, query_memory


def _service_with_claim(tmp_path, text: str, *, confirmed: bool) -> tuple[MemoryService, int]:
    db_path = tmp_path / "retrieval-parity.db"
    service = MemoryService(db_path, workspace_root=tmp_path)
    service.init_db()
    claim = service.ingest(
        text=text,
        citations=[CitationInput(source="test")],
        scope=_project_scope(str(tmp_path)),
        source_agent="retrieval-contract",
    )
    if confirmed:
        transition_claim(
            service.store,
            claim.id,
            "confirmed",
            reason="retrieval contract fixture",
            event_type="validator",
        )
    return service, claim.id


@pytest.mark.xfail(
    strict=True,
    reason="R2.1: MCP legacy retrieval sends conversational prompts to FTS5 as raw AND terms",
)
def test_conversational_recall_preserves_keyword_hits(tmp_path, monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    service, claim_id = _service_with_claim(
        tmp_path,
        "MemoryMaster explains governed claims and citations for durable recall.",
        confirmed=True,
    )
    db_path = str(service.store.db_path)
    common = {
        "db": db_path,
        "workspace": str(tmp_path),
        "retrieval_mode": "legacy",
        "include_stale": False,
        "include_conflicted": False,
        "include_candidates": False,
    }

    keyword = query_memory(query="governed claims citations", **common)
    conversational = query_memory(
        query="How does MemoryMaster explain governed claims and citations for durable recall?",
        **common,
    )
    keyword_ids = {claim["id"] for claim in keyword["claims"]}
    conversational_ids = {claim["id"] for claim in conversational["claims"]}

    assert claim_id in keyword_ids
    assert claim_id in conversational_ids


@pytest.mark.xfail(
    strict=True,
    reason="R2.1: MCP query_memory defaults to exploratory candidate recall",
)
def test_default_mcp_recall_excludes_provisional_claims(tmp_path, monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    service, claim_id = _service_with_claim(
        tmp_path,
        "provisionalrecalltoken is an unreviewed hypothesis",
        confirmed=False,
    )

    result = query_memory(
        query="provisionalrecalltoken",
        db=str(service.store.db_path),
        workspace=str(tmp_path),
    )
    returned_ids = {claim["id"] for claim in result["claims"]}

    assert claim_id not in returned_ids
    assert all(claim["status"] == "confirmed" for claim in result["claims"])
