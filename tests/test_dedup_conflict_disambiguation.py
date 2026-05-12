from __future__ import annotations

import pytest

from memorymaster.models import CitationInput
from memorymaster.service import MemoryService


class SameEmbeddingProvider:
    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0]


@pytest.fixture()
def service(tmp_path):
    svc = MemoryService(str(tmp_path / "dedup_conflict.db"))
    svc.init_db()
    svc.embedding_provider = SameEmbeddingProvider()
    return svc


def test_same_subject_predicate_different_object_values_become_conflicted(service):
    citations = [CitationInput(source="test")]
    first = service.ingest(
        text="Python version is 3.12",
        citations=citations,
        idempotency_key="dedup-conflict-first",
        subject="Python",
        predicate="version",
        object_value="3.12",
        confidence=0.9,
    )
    second = service.ingest(
        text="Python version is 3.13",
        citations=citations,
        idempotency_key="dedup-conflict-second",
        subject="Python",
        predicate="version",
        object_value="3.13",
        confidence=0.8,
    )

    result = service.dedup(threshold=0.99, dry_run=False)

    claims = {
        claim.id: claim
        for claim in service.list_claims(include_archived=True)
        if claim.id in {first.id, second.id}
    }
    assert result["claims_archived"] == 0
    assert claims[first.id].status == "conflicted"
    assert claims[second.id].status == "conflicted"
