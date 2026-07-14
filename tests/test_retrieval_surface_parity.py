"""Red contracts for governed recall behavior at the MCP surface."""

from __future__ import annotations

from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.surfaces.mcp_server import (
    _project_scope,
    query_for_context,
    query_memory,
    volunteer_context,
)


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


def _claim_ids(result: dict) -> set[int]:
    return {
        int(claim.get("id", claim.get("claim_id")))
        for claim in result.get("claims", [])
    }


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
    keyword_ids = _claim_ids(keyword)
    conversational_ids = _claim_ids(conversational)

    assert claim_id in keyword_ids
    assert claim_id in conversational_ids


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
    returned_ids = _claim_ids(result)

    assert claim_id not in returned_ids
    assert all(claim["status"] == "confirmed" for claim in result["claims"])


def test_exploratory_mode_is_explicit_and_annotated(tmp_path, monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    service, claim_id = _service_with_claim(
        tmp_path,
        "exploratorycontracttoken is an unreviewed hypothesis",
        confirmed=False,
    )

    result = query_memory(
        query="exploratorycontracttoken",
        db=str(service.store.db_path),
        workspace=str(tmp_path),
        trust_mode="exploratory",
    )

    rows = {
        result["claims"][row["claim_index"]]["id"]: row
        for row in result["rows_data"]
    }
    assert claim_id in rows
    assert rows[claim_id]["annotation"]["active"] is False
    assert rows[claim_id]["annotation"]["status"] == "candidate"


def test_context_surfaces_share_trusted_default_ids(tmp_path, monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    service, confirmed_id = _service_with_claim(
        tmp_path,
        "surfaceparitytoken belongs to a confirmed governed claim",
        confirmed=True,
    )
    candidate = service.ingest(
        text="surfaceparitytoken belongs to an unreviewed candidate",
        citations=[CitationInput(source="test")],
        scope=_project_scope(str(tmp_path)),
        source_agent="retrieval-contract",
    )
    common = {
        "query": "surfaceparitytoken",
        "db": str(service.store.db_path),
        "workspace": str(tmp_path),
        "detail_level": "summary",
    }

    query_result = query_memory(**common)
    context_result = query_for_context(**common)
    volunteer_result = volunteer_context(**common, min_confidence=0.0)

    id_sets = [
        _claim_ids(result)
        for result in (query_result, context_result, volunteer_result)
    ]
    assert id_sets == [{confirmed_id}, {confirmed_id}, {confirmed_id}]
    assert candidate.id not in set().union(*id_sets)
