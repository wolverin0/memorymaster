"""Regression gates for temporal truth and scope-safe mutable-state capture.

These tests cover normal recall, cached/vector/graph rehydration, mutable
provider-state intake, and governed supersession intent. Read when changing
claim validity, scope defaults, or the ingest/recall trust boundary.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.knowledge.entity_graph import EntityGraph
from memorymaster.recall.planner import RetrievalPlan
from memorymaster.recall.qdrant_backend import QdrantCandidate, claim_content_hash


PAST = "2020-01-01T00:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"
NO_VECTOR = lambda _query, _claims: {}  # noqa: E731


@pytest.fixture
def service(tmp_path: Path) -> MemoryService:
    svc = MemoryService(tmp_path / "temporal.db", workspace_root=tmp_path)
    svc.init_db()
    return svc


def _ingest(
    service: MemoryService,
    text: str,
    *,
    scope: str = "project:test",
    valid_from: str | None = None,
    valid_until: str | None = None,
    volatility: str = "medium",
    supersedes_claim_id: int | None = None,
):
    return service.ingest(
        text=text,
        citations=[CitationInput(source="pytest", locator=scope)],
        claim_type="constraint",
        scope=scope,
        volatility=volatility,
        valid_from=valid_from,
        valid_until=valid_until,
        supersedes_claim_id=supersedes_claim_id,
        source_agent="pytest",
    )


def _confirm(service: MemoryService, claim):
    return service.store.apply_status_transition(
        claim,
        to_status="confirmed",
        reason="temporal regression fixture",
        event_type="validator",
    )


@pytest.mark.parametrize("mode", ["legacy", "hybrid"])
def test_normal_recall_excludes_expired_confirmed_claim(service, mode: str) -> None:
    expired = _confirm(
        service,
        _ingest(
            service,
            "Temporary provider quota is exhausted.",
            valid_from="2019-01-01T00:00:00+00:00",
            valid_until=PAST,
        ),
    )

    rows = service.query_rows(
        "provider quota exhausted",
        retrieval_mode=mode,
        vector_hook=NO_VECTOR if mode == "hybrid" else None,
    )

    assert expired.id not in {row["claim"].id for row in rows}


def test_normal_recall_excludes_not_yet_valid_claim(service) -> None:
    future = _confirm(
        service,
        _ingest(service, "Future provider migration policy.", valid_from=FUTURE),
    )

    rows = service.query_rows("future provider migration policy")

    assert future.id not in {row["claim"].id for row in rows}


def test_cache_rehydration_rechecks_time_without_a_database_write(service) -> None:
    expired = _confirm(
        service,
        _ingest(
            service,
            "Cached temporary quota state.",
            valid_from="2019-01-01T00:00:00+00:00",
            valid_until=PAST,
        ),
    )

    assert service._rehydrate_cached_rows([{"id": expired.id, "score": 1.0}]) == []


def test_qdrant_rehydration_rechecks_time(service) -> None:
    expired = _confirm(
        service,
        _ingest(
            service,
            "Vector temporary quota state.",
            valid_from="2019-01-01T00:00:00+00:00",
            valid_until=PAST,
        ),
    )
    candidate = QdrantCandidate(
        claim_id=expired.id,
        content_hash=claim_content_hash(expired),
        score=0.9,
    )
    plan = RetrievalPlan(
        query_text="quota",
        search_text="quota",
        limit=5,
        trust_mode="trusted",
        statuses=("confirmed",),
        requested_mode="qdrant",
        effective_mode="qdrant",
        containment_reason=None,
        allow_sensitive=False,
        scope_allowlist=None,
        requesting_agent=None,
        query_type=None,
        retrieval_profile=None,
    )

    assert service._authoritative_qdrant_claim(candidate, plan, None, None) is None


def test_graph_traversal_ignores_expired_support(service) -> None:
    expired = _confirm(
        service,
        _ingest(
            service,
            "Alice participates in Temporary Atlas.",
            valid_from="2019-01-01T00:00:00+00:00",
            valid_until=PAST,
        ),
    )
    graph = EntityGraph(str(service.store.db_path))
    payload = {
        "entities": [
            {"name": "Alice", "type": "person", "aliases": []},
            {"name": "Temporary Atlas", "type": "project", "aliases": []},
        ],
        "relations": [
            {
                "source": "Alice",
                "target": "Temporary Atlas",
                "relation": "participates_in",
            }
        ],
    }
    with patch(
        "memorymaster.knowledge.entity_graph._llm_chat",
        return_value=json.dumps(payload),
    ):
        graph.extract_and_link(expired.id, expired.text)

    assert expired.id not in graph.find_related_claims(["Alice"])


def test_mutable_project_state_without_structured_expiry_is_high_volatility(
    service,
) -> None:
    claim = _ingest(
        service,
        "The Codex quota is currently exhausted until 2026-08-08.",
        volatility="low",
    )

    assert claim.volatility == "high"
    events = service.list_events(
        claim_id=claim.id, event_type="policy_decision", limit=20
    )
    assert any(event.details == "mutable_state:forced_high_volatility" for event in events)


def test_structured_expiry_preserves_requested_volatility(service) -> None:
    claim = _ingest(
        service,
        "The Codex quota is currently available.",
        valid_until=FUTURE,
        volatility="low",
    )

    assert claim.volatility == "low"


def test_mutable_state_cannot_be_ingested_as_global(service) -> None:
    with pytest.raises(ValueError, match="mutable.*global|global.*mutable"):
        _ingest(
            service,
            "The Codex quota is currently exhausted.",
            scope="global",
            volatility="low",
        )


def test_durable_quota_architecture_is_not_misclassified(service) -> None:
    claim = _ingest(
        service,
        "The scheduler enforces a provider quota budget before each autonomous cycle.",
        scope="global",
        volatility="low",
    )

    assert claim.volatility == "low"


def test_supersession_intent_queues_review_without_rewriting_truth(service) -> None:
    old = _confirm(service, _ingest(service, "Use the old provider policy."))

    replacement = _ingest(
        service,
        "Use the replacement provider policy.",
        supersedes_claim_id=old.id,
    )

    assert replacement.status == "candidate"
    assert service.store.get_claim(old.id).status == "confirmed"
    events = service.list_events(
        claim_id=old.id, event_type="policy_decision", limit=20
    )
    proposal = next(
        event for event in events
        if event.details == "steward_proposal:superseded_candidate"
    )
    assert json.loads(proposal.payload_json)["replaced_by_claim_id"] == replacement.id


@pytest.mark.parametrize("target", [999999, "other_scope"])
def test_supersession_intent_rejects_unknown_or_cross_scope_target(
    service, target
) -> None:
    target_id = target
    if target == "other_scope":
        target_id = _ingest(
            service,
            "Other project policy.",
            scope="project:other",
        ).id

    with pytest.raises(ValueError, match="supersession target"):
        _ingest(
            service,
            "Replacement cannot cross the identity boundary.",
            supersedes_claim_id=int(target_id),
        )
