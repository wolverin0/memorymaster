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


def test_second_duplicate_archives_without_overwriting_reciprocal_link(
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
    first = _claim(service, "first duplicate", "first-duplicate")
    run(service.store, min_citations=0, min_score=0.0)
    second = _claim(service, "second duplicate", "second-duplicate")

    result = run(service.store, min_citations=0, min_score=0.0)

    refreshed_confirmed = service.store.get_claim(
        confirmed.id, include_citations=False,
    )
    refreshed_second = service.store.get_claim(
        second.id, include_citations=False,
    )
    assert result["archived_duplicates"] == 1
    assert refreshed_confirmed.supersedes_claim_id == first.id
    assert refreshed_second.status == "archived"
    assert refreshed_second.replaced_by_claim_id == confirmed.id


def test_higher_score_never_retires_a_confirmed_rival_with_another_value(tmp_path: Path) -> None:
    """T-1877 (review F5): true .79 vs false .99 with four citations and no supersession link."""
    service = MemoryService(str(tmp_path / "validator.db"))
    service.init_db()
    true_claim = service.ingest(
        text="the router firmware is 7.18.2",
        citations=[CitationInput(source="validator-rival-true")],
        idempotency_key="rival-true", subject="router", predicate="firmware", object_value="7.18.2",
    )
    transition_claim(service.store, true_claim.id, "confirmed", reason="fixture", event_type="validator")
    service.store.set_confidence(true_claim.id, 0.79, details="fixture")
    false_claim = service.ingest(
        text="the router firmware is 9.99.9, as documented in the structured release notes of the router",
        citations=[CitationInput(source=f"validator-rival-false-{n}") for n in range(4)],
        idempotency_key="rival-false", subject="router", predicate="firmware", object_value="9.99.9",
        confidence=0.99,
    )

    result = run(service.store, min_citations=0, min_score=0.0)

    assert service.store.get_claim(true_claim.id, include_citations=False).status == "confirmed"
    assert service.store.get_claim(false_claim.id, include_citations=False).status == "conflicted"
    assert result["conflicted"] >= 1 and result["superseded"] == 0
