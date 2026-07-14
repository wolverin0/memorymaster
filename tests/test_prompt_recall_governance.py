from __future__ import annotations

from pathlib import Path

import pytest

import memorymaster.recall.context_hook as context_hook
from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall.context_hook import recall


def test_prompt_recall_uses_trusted_scope_governance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "prompt-recall.db"
    monkeypatch.setenv("MEMORYMASTER_SCOPE_DEFAULT", "project:allowed")
    monkeypatch.setenv("MEMORYMASTER_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.delenv("QDRANT_URL", raising=False)
    service = MemoryService(db_path, workspace_root=tmp_path)
    service.init_db()

    confirmed = service.ingest(
        "plannerboundary confirmed allowed fact",
        [CitationInput(source="test://prompt", locator="confirmed")],
        scope="project:allowed",
    )
    transition_claim(service.store, confirmed.id, "confirmed", "test fixture")
    candidate = service.ingest(
        "plannerboundary candidate allowed draft",
        [CitationInput(source="test://prompt", locator="candidate")],
        scope="project:allowed",
    )
    foreign = service.ingest(
        "plannerboundary confirmed foreign fact",
        [CitationInput(source="test://prompt", locator="foreign")],
        scope="project:foreign",
    )
    transition_claim(service.store, foreign.id, "confirmed", "test fixture")

    rendered, claim_ids = recall(
        "plannerboundary",
        db_path=str(db_path),
        skip_qdrant=True,
        return_ids=True,
    )

    assert confirmed.id in claim_ids
    assert "confirmed allowed fact" in rendered
    assert candidate.id not in claim_ids
    assert "candidate allowed draft" not in rendered
    assert foreign.id not in claim_ids
    assert "confirmed foreign fact" not in rendered


def test_downstream_graph_candidates_cannot_bypass_prompt_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "prompt-graph.db"
    monkeypatch.setenv("MEMORYMASTER_SCOPE_DEFAULT", "project:allowed")
    monkeypatch.setenv("MEMORYMASTER_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH", "1")
    monkeypatch.setenv("MEMORYMASTER_RECALL_GRAPH_CANDIDATES", "1")
    monkeypatch.setenv("MEMORYMASTER_RECALL_W_GRAPH", "1")
    monkeypatch.delenv("QDRANT_URL", raising=False)
    service = MemoryService(db_path, workspace_root=tmp_path)
    service.init_db()

    anchor = service.ingest(
        "graphpolicyanchor confirmed allowed fact",
        [CitationInput(source="test://graph", locator="anchor")],
        scope="project:allowed",
    )
    transition_claim(service.store, anchor.id, "confirmed", "test fixture")
    candidate = service.ingest(
        "downstream candidate draft",
        [CitationInput(source="test://graph", locator="candidate")],
        scope="project:allowed",
    )
    foreign = service.ingest(
        "downstream foreign fact",
        [CitationInput(source="test://graph", locator="foreign")],
        scope="project:foreign",
    )
    transition_claim(service.store, foreign.id, "confirmed", "test fixture")
    private = service.ingest(
        "downstream private fact",
        [CitationInput(source="test://graph", locator="private")],
        scope="project:allowed",
        visibility="private",
    )
    transition_claim(service.store, private.id, "confirmed", "test fixture")

    monkeypatch.setattr(
        context_hook,
        "_graph_reached_claim_distance",
        lambda _query, _store: {candidate.id: 1, foreign.id: 1, private.id: 1},
    )
    rendered, claim_ids = context_hook.recall(
        "graphpolicyanchor",
        db_path=str(db_path),
        skip_qdrant=True,
        return_ids=True,
    )

    assert claim_ids == [anchor.id]
    assert "confirmed allowed fact" in rendered
    assert "candidate draft" not in rendered
    assert "foreign fact" not in rendered
    assert "private fact" not in rendered
