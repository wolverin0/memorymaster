from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.stores._storage_shared import ConcurrentModificationError
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


def _service(tmp_path: Path) -> MemoryService:
    service = MemoryService(tmp_path / "lifecycle-supersede.db", workspace_root=tmp_path)
    service.init_db()
    return service


def _claim(service: MemoryService, text: str) -> int:
    claim = service.ingest(
        text=text,
        citations=[CitationInput(source="tests/test_lifecycle_supersede_invariant.py", locator=text, excerpt=text)],
        subject="lifecycle",
        predicate="invariant",
        object_value=text,
        scope="project:memorymaster",
    )
    return int(claim.id)


def _assert_supersede_pairs_are_symmetric(service: MemoryService) -> None:
    superseded_claims = service.store.list_claims(status="superseded", limit=100)
    assert superseded_claims

    for old_claim in superseded_claims:
        assert old_claim.replaced_by_claim_id is not None
        target = service.store.get_claim(old_claim.replaced_by_claim_id, include_citations=False)
        assert target is not None
        assert target.supersedes_claim_id == old_claim.id


def test_supersede_sets_both_sides(tmp_path: Path) -> None:
    service = _service(tmp_path)
    old_id = _claim(service, "Original lifecycle claim")
    new_id = _claim(service, "Replacement lifecycle claim")

    service.store.mark_superseded(old_id, new_id, "replacement")

    old = service.store.get_claim(old_id, include_citations=False)
    new = service.store.get_claim(new_id, include_citations=False)
    assert old is not None
    assert new is not None
    assert old.replaced_by_claim_id == new.id
    assert new.supersedes_claim_id == old.id


def test_no_orphan_supersedes(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first_id = _claim(service, "First lifecycle claim")
    second_id = _claim(service, "Second lifecycle claim")
    third_id = _claim(service, "Third lifecycle claim")

    service.store.mark_superseded(first_id, second_id, "first replacement")
    service.store.mark_superseded(second_id, third_id, "second replacement")

    _assert_supersede_pairs_are_symmetric(service)


def test_concurrent_supersede_safety(tmp_path: Path) -> None:
    service = _service(tmp_path)
    old_id = _claim(service, "Raced lifecycle claim")
    first_new_id = _claim(service, "First raced replacement")
    second_new_id = _claim(service, "Second raced replacement")

    service.store.mark_superseded(old_id, first_new_id, "first replacement")
    with pytest.raises(ConcurrentModificationError):
        service.store.mark_superseded(old_id, second_new_id, "second replacement")

    old = service.store.get_claim(old_id, include_citations=False)
    first_new = service.store.get_claim(first_new_id, include_citations=False)
    second_new = service.store.get_claim(second_new_id, include_citations=False)
    assert old is not None
    assert first_new is not None
    assert second_new is not None
    assert old.replaced_by_claim_id == first_new_id
    assert first_new.supersedes_claim_id == old_id
    assert second_new.supersedes_claim_id is None
    _assert_supersede_pairs_are_symmetric(service)
