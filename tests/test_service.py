import pytest
from types import SimpleNamespace

from memorymaster.core import observability
from memorymaster.core.generated_artifacts import GENERATED_CLOSER, GENERATED_OPENER
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


def test_bound_recall_activity_cannot_recreate_database(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DB_RETRIES", "0")
    db = tmp_path / "removed-after-session-binding.db"
    service = MemoryService.__new__(MemoryService)
    service.store = SimpleNamespace(db_path=db)
    service.session_id = 1
    service.source_agent = "missing-db-client"
    before = observability.metric_value("recalls_queried_total", source_agent=service.source_agent)
    service._emit_recall_telemetry()
    assert observability.metric_value("recalls_queried_total", source_agent=service.source_agent) == before + 1
    assert not db.exists()


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


@pytest.mark.parametrize("match_path", ["producer", "content_hash", "concurrent"])
@pytest.mark.parametrize("field", ["text", "subject", "predicate", "object_value", "citation"])
def test_legacy_generated_dedup_key_must_not_revive(service, monkeypatch, match_path, field):
    import hashlib

    clean = "A newly authored clean claim."
    key = "producer-key"
    if match_path == "content_hash":
        key = "hash-" + hashlib.sha256(f"{clean.lower()}|project|".encode()).hexdigest()[:16]
    generated = GENERATED_OPENER + "Recalled falsehood." + GENERATED_CLOSER
    fields = {field: generated} if field not in {"text", "citation"} else {}
    old = service.store.create_claim(
        generated if field == "text" else "Previously authored claim.",
        [CitationInput(source="fixture", excerpt=generated if field == "citation" else None)],
        idempotency_key=key, **fields,
    )
    service.store.apply_status_transition(old, to_status="archived", reason="fixture", event_type="transition")
    if match_path == "concurrent":
        # Simulate a winner becoming visible only inside atomic create_claim.
        monkeypatch.setattr(service.store, "get_claim_by_idempotency_key", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="generated"):
        service.ingest(clean, [CitationInput(source="fixture")],
                       idempotency_key=None if match_path == "content_hash" else key)
    persisted = service.store.get_claim(old.id)
    assert persisted.status == "archived"
    assert persisted.text == old.text


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


def test_content_dedup_finds_a_copy_whatever_key_the_original_used(service):
    """T-1865 F14: the content fingerprint, not the first copy's key, identifies a copy."""
    cites = [CitationInput(source="fixture")]
    first = service.ingest("Backups rotate after thirty days.", cites, idempotency_key="producer-key")
    unkeyed = service.ingest("  BACKUPS rotate after thirty days.  ", cites)
    assert unkeyed.id == first.id
    other_scope = service.ingest("Backups rotate after thirty days.", cites, scope="project:other")
    assert other_scope.id != first.id


def test_restricted_list_claims_pages_past_other_agents_private_rows(service):
    """T-2070: private rows of other agents must not use up a restricted caller's limit."""
    cite = [CitationInput(source="fixture")]
    for i in range(4):
        service.ingest(f"private note {i}", cite, visibility="private", source_agent=f"agent{i}", confidence=0.99)
    public = service.ingest("public note", cite, confidence=0.1)
    rows = service.list_claims(limit=2, requesting_agent="someone-else")
    assert [claim.id for claim in rows] == [public.id]


def test_authorize_claims_applies_tenant_visibility_and_sensitivity():
    from memorymaster.core.service import authorize_claims

    def claim(**kw):
        base = dict(text="plain", subject=None, predicate=None, object_value=None,
                    visibility="public", source_agent="a", tenant_id=None)
        return SimpleNamespace(**{**base, **kw})

    rows = [claim(), claim(visibility="private"), claim(tenant_id="other"), claim(text="x [REDACTED:api_key]")]
    kept = authorize_claims(rows, requesting_agent="viewer", tenant_id=None)
    assert kept == [rows[0], rows[2]]
    assert authorize_claims(rows, requesting_agent="viewer", tenant_id="t1") == []
    assert authorize_claims(rows, include_sensitive=True) == rows


def test_retrieve_dense_plan_reports_lexical_fallback_reason(tmp_path, monkeypatch):
    """retrieve() answers a "dense" plan lexically, with the reason, when dense yields nothing (T-2115)."""
    from memorymaster.core.lifecycle import transition_claim
    from memorymaster.recall import dense_recall
    from memorymaster.recall.planner import RetrievalRequest

    monkeypatch.setenv("MEMORYMASTER_DEFAULT_DB", str(tmp_path / "dense-fallback.db"))  # the db the index covers
    svc = MemoryService(str(tmp_path / "dense-fallback.db"), workspace_root=tmp_path)
    svc.init_db()
    claim = svc.ingest("dense fallback marker", [CitationInput(source="test://dense")], scope="project:x", source_agent="seed")
    transition_claim(svc.store, claim.id, "confirmed", reason="fixture", event_type="validator")

    def down(*_args, **_kwargs):
        raise dense_recall.DenseUnavailable("down")

    monkeypatch.setattr(dense_recall, "search", down)
    result = svc.retrieve(RetrievalRequest(query_text="dense fallback marker", retrieval_mode="dense", scope_allowlist=("project:x",)))

    assert result.plan.requested_mode == "dense"
    assert result.plan.effective_mode == "legacy"
    assert "unavailable" in result.plan.containment_reason
    assert [row["claim"].id for row in result.rows] == [claim.id]
