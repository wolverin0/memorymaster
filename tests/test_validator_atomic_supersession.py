from __future__ import annotations

from pathlib import Path

from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.jobs.validator import run


def _claim(service: MemoryService, text: str, key: str):
    return service.ingest(
        text=text,
        citations=[CitationInput(source="validator-atomicity")],
        idempotency_key=key,
        subject="validator-atomicity",
        predicate="keeps",
        object_value="same-value",
    )


def test_duplicate_validation_writes_reciprocal_supersession(
    tmp_path: Path,
) -> None:
    service = MemoryService(str(tmp_path / "validator.db"))
    service.init_db()
    confirmed = _claim(service, "confirmed winner", "confirmed-winner")
    transition_claim(
        service.store,
        confirmed.id,
        "confirmed",
        reason="fixture",
        event_type="validator",
    )
    duplicate = _claim(service, "candidate duplicate", "candidate-duplicate")

    result = run(service.store, min_citations=0, min_score=0.0)

    refreshed_duplicate = service.store.get_claim(
        duplicate.id,
        include_citations=False,
    )
    refreshed_confirmed = service.store.get_claim(
        confirmed.id,
        include_citations=False,
    )
    assert result["superseded"] == 1
    assert refreshed_duplicate.status == "superseded"
    assert refreshed_duplicate.replaced_by_claim_id == confirmed.id
    assert refreshed_confirmed.supersedes_claim_id == duplicate.id
