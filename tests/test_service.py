import pytest

from memorymaster.core.generated_artifacts import GENERATED_CLOSER, GENERATED_OPENER
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


@pytest.fixture
def service(tmp_path):
    service = MemoryService(str(tmp_path / "guard.db"), workspace_root=tmp_path)
    service.init_db()
    return service


def test_generated_only_cannot_revive_archived_producer_key(service):
    claim = service.ingest("Previously authored claim.", [CitationInput(source="fixture")], idempotency_key="producer-key")
    service.store.apply_status_transition(claim, to_status="archived", reason="fixture", event_type="transition")
    with pytest.raises(ValueError, match="generated"):
        service.ingest(f"{GENERATED_OPENER}Previously authored claim.{GENERATED_CLOSER}", [CitationInput(source="fixture")], idempotency_key="producer-key")
    assert service.store.get_claim(claim.id).status == "archived"


def test_mixed_atomic_claim_retains_one_authored_span_and_recomputes_dedup(service):
    text = f"A durable authored claim.\n{GENERATED_OPENER}Generated falsehood.{GENERATED_CLOSER}"
    claim = service.ingest(text, [CitationInput(source="fixture", excerpt="A durable authored claim.")])
    assert claim.text == "A durable authored claim."
    replay = service.ingest("A durable authored claim.", [CitationInput(source="fixture")])
    assert claim.id == replay.id


@pytest.mark.parametrize("fields", [
    {"subject": f"{GENERATED_OPENER}laundered{GENERATED_CLOSER}"},
    {"citations": [CitationInput(source="fixture", excerpt=f"{GENERATED_OPENER}false evidence{GENERATED_CLOSER}")]},
    {"text": f"Prefix claim.{GENERATED_OPENER}falsehood{GENERATED_CLOSER}Suffix claim."},
    {"text": f"Prefix claim.{GENERATED_OPENER}falsehood{GENERATED_CLOSER}", "idempotency_key": "tainted-key"},
])
def test_generated_fields_cannot_launder_an_atomic_claim(service, fields):
    args = {"text": "Authored durable claim.", "citations": [CitationInput(source="fixture")]}
    args.update(fields)
    with pytest.raises(ValueError, match="generated"):
        service.ingest(**args)

