from __future__ import annotations

from memorymaster.capture import CaptureRepository
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.evaluation.evidence_rehydration import rehydrate_claim_evidence


def _claim(service, text, scope="project:synthetic", status="confirmed"):
    claim = service.ingest(text=text, citations=[CitationInput(source="synthetic", locator=text)], scope=scope)
    if status == "confirmed":
        service.store.apply_status_transition(claim, to_status="confirmed", reason="fixture", event_type="validator")
    return service.store.get_claim(claim.id)


def _linked(service, claim, text, source_key):
    source = service.upsert_external_source(source_type="synthetic", display_name="Synthetic", config_json={})
    item = service.upsert_source_item(source_id=source.id, source_item_id=source_key, item_type="text", text=text)
    evidence = service.add_evidence_item(source_item_id=item.id, evidence_type="text", text=text)
    CaptureRepository(service.store).link_claim_evidence(claim_id=claim.id, evidence_item_id=evidence.id)
    return item, evidence


def test_exact_active_evidence_is_mapped_from_confirmed_authorized_claim(tmp_path):
    service = MemoryService(tmp_path / "r.db", workspace_root=tmp_path)
    service.init_db()
    claim = _claim(service, "Synthetic claim alpha")
    _, evidence = _linked(service, claim, "Exact synthetic excerpt alpha.", "alpha")

    result = rehydrate_claim_evidence(service, [claim.id], scope_allowlist=["project:synthetic"])

    assert result["fallback_reason"] == "none"
    assert result["claims"][0]["claim_id"] == claim.id
    assert result["claims"][0]["evidence"][0] == {"evidence_id": evidence.id, "excerpt": "Exact synthetic excerpt alpha."}


def test_graph_signal_is_navigation_only_and_revalidated(tmp_path):
    service = MemoryService(tmp_path / "r.db", workspace_root=tmp_path)
    service.init_db()
    seed = _claim(service, "Seed")
    related = _claim(service, "Related")
    candidate = _claim(service, "Candidate", status="candidate")
    cross = _claim(service, "Cross", scope="project:other")
    _linked(service, seed, "Seed evidence", "seed")
    _linked(service, related, "Related evidence", "related")

    result = rehydrate_claim_evidence(
        service, [seed.id], graph_signal_claim_ids=[related.id, candidate.id, cross.id],
        scope_allowlist=["project:synthetic"], max_graph_claims=1,
    )

    assert [row["claim_id"] for row in result["claims"]] == [seed.id, related.id]
    assert result["graph_signal_ids"] == [related.id]


def test_retired_source_and_sensitive_claim_never_rehydrate(tmp_path):
    service = MemoryService(tmp_path / "r.db", workspace_root=tmp_path)
    service.init_db()
    retired = _claim(service, "Retired")
    item, _ = _linked(service, retired, "Retired evidence", "retired")
    CaptureRepository(service.store).retire_source(item.id, reason="fixture")
    sensitive = _claim(service, "Legacy sensitive placeholder")
    with service.store.connect() as conn:
        conn.execute(
            "UPDATE claims SET text=? WHERE id=?",
            ("secret token sk-test-abcdefghijklmnopqrstuvwxyz", sensitive.id),
        )
        conn.commit()
    sensitive = service.store.get_claim(sensitive.id)
    _linked(service, sensitive, "Sensitive evidence", "sensitive")

    result = rehydrate_claim_evidence(service, [retired.id, sensitive.id], scope_allowlist=["project:synthetic"])

    assert result["claims"] == []
    assert result["fallback_reason"] == "no_authorized_evidence"


def test_missing_evidence_reports_diagnostic_without_inventing_excerpt(tmp_path):
    service = MemoryService(tmp_path / "r.db", workspace_root=tmp_path)
    service.init_db()
    claim = _claim(service, "No linked evidence")

    result = rehydrate_claim_evidence(service, [claim.id], scope_allowlist=["project:synthetic"])

    assert result["claims"] == [{"claim_id": claim.id, "evidence": []}]
    assert result["fallback_reason"] == "insufficient_evidence"


def test_rehydration_is_bounded_and_replay_safe(tmp_path):
    service = MemoryService(tmp_path / "r.db", workspace_root=tmp_path)
    service.init_db()
    claims = [_claim(service, f"Claim {index}") for index in range(3)]
    for index, claim in enumerate(claims):
        _linked(service, claim, f"Evidence {index}", f"source-{index}")

    first = rehydrate_claim_evidence(service, [row.id for row in claims], scope_allowlist=["project:synthetic"], max_claims=2)
    second = rehydrate_claim_evidence(service, [row.id for row in claims], scope_allowlist=["project:synthetic"], max_claims=2)

    assert first == second
    assert len(first["claims"]) == 2
