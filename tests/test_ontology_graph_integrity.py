"""Trusted graph traversal, ontology validation, and replay-safety contracts.

These tests cover personal-v1 extraction, source retirement, scope isolation,
edge support deduplication, worker diagnostics, and additive custom ontology.
They are the release gate for governed graph provenance; failures must block
trusted graph activation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from memorymaster import forget, remember
from memorymaster.capture import CaptureRepository
from memorymaster.capture.adapters import CaptureRejected
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.knowledge.entity_graph import EntityGraph
from memorymaster.knowledge.graph_extraction import extract_confirmed_claim_graph
from memorymaster.knowledge.ontology import load_ontology


def _service(tmp_path: Path) -> tuple[MemoryService, Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = tmp_path / "graph.db"
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()
    return service, db, workspace


def _captured_claim(
    service: MemoryService,
    db: Path,
    workspace: Path,
    *,
    text: str,
    scope: str,
):
    receipt = remember(text=text, scope=scope, db=db, workspace=workspace)
    evidence_id = int(receipt.evidence["id"])
    claim = service.ingest(
        text,
        [CitationInput(source="fixture", locator=f"evidence:{evidence_id}")],
        scope=scope,
    )
    CaptureRepository(service.store).link_claim_evidence(
        claim_id=claim.id, evidence_item_id=evidence_id
    )
    claim = service.store.apply_status_transition(
        claim,
        to_status="confirmed",
        reason="trusted graph fixture",
        event_type="validator",
    )
    return receipt, claim


def _extract(graph: EntityGraph, claim, payload: dict) -> list[str]:
    with patch(
        "memorymaster.knowledge.entity_graph._llm_chat",
        return_value=json.dumps(payload),
    ):
        return graph.extract_and_link(claim.id, claim.text)


def _relationship(left: str, right: str) -> dict:
    return {
        "entities": [
            {"name": left, "type": "person", "aliases": []},
            {"name": right, "type": "project", "aliases": []},
        ],
        "relations": [
            {"source": left, "target": right, "relation": "participates_in"}
        ],
    }


def test_replaying_claim_does_not_reinforce_edge_twice(tmp_path: Path) -> None:
    service, db, workspace = _service(tmp_path)
    _, claim = _captured_claim(
        service,
        db,
        workspace,
        text="Alice participates in Project Atlas.",
        scope="project:a",
    )
    graph = EntityGraph(str(db))

    assert _extract(graph, claim, _relationship("Alice", "Project Atlas"))
    assert _extract(graph, claim, _relationship("Alice", "Project Atlas"))

    with service.store.connect() as conn:
        edge = conn.execute("SELECT weight FROM entity_edges").fetchone()
        supports = conn.execute("SELECT COUNT(*) FROM entity_edge_supports").fetchone()
    assert edge["weight"] == pytest.approx(1.0)
    assert supports[0] == 1


def test_new_supporting_claim_reinforces_existing_edge(tmp_path: Path) -> None:
    service, db, workspace = _service(tmp_path)
    _, first = _captured_claim(
        service, db, workspace, text="Alice joined Atlas.", scope="project:a"
    )
    _, second = _captured_claim(
        service, db, workspace, text="Alice contributes to Atlas.", scope="project:a"
    )
    graph = EntityGraph(str(db))
    payload = _relationship("Alice", "Atlas")

    _extract(graph, first, payload)
    _extract(graph, second, payload)

    with service.store.connect() as conn:
        edge = conn.execute("SELECT weight FROM entity_edges").fetchone()
        supports = conn.execute("SELECT COUNT(*) FROM entity_edge_supports").fetchone()
    assert edge["weight"] == pytest.approx(1.1)
    assert supports[0] == 2


def test_retired_source_support_disappears_from_trusted_graph(tmp_path: Path) -> None:
    service, db, workspace = _service(tmp_path)
    receipt, claim = _captured_claim(
        service,
        db,
        workspace,
        text="Alice participates in Atlas.",
        scope="project:a",
    )
    graph = EntityGraph(str(db))
    _extract(graph, claim, _relationship("Alice", "Atlas"))
    assert claim.id in graph.find_related_claims(["Alice"], scope_allowlist=["project:a"])

    forget(
        source_item_id=int(receipt.source_item["id"]),
        apply=True,
        db=db,
        workspace=workspace,
    )

    assert claim.id not in graph.find_related_claims(
        ["Alice"], scope_allowlist=["project:a"]
    )


def test_cross_scope_support_cannot_bridge_traversal(tmp_path: Path) -> None:
    service, db, workspace = _service(tmp_path)
    _, allowed = _captured_claim(
        service, db, workspace, text="Alice uses Shared.", scope="project:a"
    )
    _, denied = _captured_claim(
        service, db, workspace, text="Shared depends on Secret.", scope="project:b"
    )
    graph = EntityGraph(str(db))
    _extract(graph, allowed, _relationship("Alice", "Shared"))
    _extract(graph, denied, _relationship("Shared", "Secret"))

    result = graph.find_related_claims(
        ["Alice"], hops=2, scope_allowlist=["project:a"]
    )

    assert allowed.id in result
    assert denied.id not in result


def test_unknown_ontology_values_fail_closed_without_partial_graph(
    tmp_path: Path,
) -> None:
    service, db, workspace = _service(tmp_path)
    _, claim = _captured_claim(
        service, db, workspace, text="Alice knows Bob.", scope="project:a"
    )
    graph = EntityGraph(str(db))
    payload = {
        "entities": [
            {"name": "Alice", "type": "person", "aliases": []},
            {"name": "Bob", "type": "alien", "aliases": []},
        ],
        "relations": [{"source": "Alice", "target": "Bob", "relation": "knows"}],
    }

    assert _extract(graph, claim, payload) == []
    assert set(graph.last_diagnostics) == {
        "unknown_entity_type:alien",
        "unknown_relation:knows",
    }
    assert graph.get_stats()["entities"] == 0


def test_graph_worker_surfaces_ontology_diagnostics(tmp_path: Path) -> None:
    service, db, workspace = _service(tmp_path)
    receipt, claim = _captured_claim(
        service, db, workspace, text="Alice knows Bob.", scope="project:a"
    )
    repository = CaptureRepository(service.store)
    digest = hashlib.sha256(
        f"claim:{claim.id}:{claim.updated_at}".encode("utf-8")
    ).hexdigest()
    repository.queue_job(
        source_item_id=int(receipt.source_item["id"]),
        content_hash=digest,
        stage="extract_graph",
    )
    job = repository.lease_jobs(
        owner="test", stages=("extract_graph",), limit=1
    )[0]

    with patch(
        "memorymaster.knowledge.entity_graph._llm_chat",
        return_value='{"entities":[],"relations":[{"relation":"knows"}]}',
    ), pytest.raises(CaptureRejected, match="unknown_relation:knows") as exc:
        extract_confirmed_claim_graph(service, repository, job)

    assert exc.value.code == "ontology_validation_failed"


def test_custom_ontology_is_additive_and_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "ontology.json"
    custom.write_text(
        json.dumps(
            {
                "version": "home-v1",
                "entity_types": ["pet"],
                "relations": [
                    {"name": "cares_for", "directed": True, "symmetric": False}
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MEMORYMASTER_ONTOLOGY_FILE", str(custom))

    ontology = load_ontology()

    assert ontology.version == "personal-v1+home-v1"
    assert {"person", "pet"} <= ontology.entity_types
    assert {"related_to", "cares_for"} <= ontology.relations.keys()
