"""Source-aware Dreaming must not gain trust from candidate text alone."""

from dataclasses import replace
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.dreaming.models import DreamCandidate
from memorymaster.dreaming.providers import consolidation_from_raw, consolidation_prompt
from memorymaster.dreaming.source_review import (
    CHECKS, bind_source, parse_review, record_review, review_allows_confirmation,
)
from memorymaster.govern.jobs import validator
from memorymaster.govern.llm_steward import _confirm_candidate_cas


@pytest.fixture
def service(tmp_path):
    result = MemoryService(tmp_path / "claims.db", workspace_root=tmp_path)
    result.init_db()
    return result


def candidate():
    return DreamCandidate("dc-test", "The importer limit is 30 days.", "fact",
                          "importer", "limit", "30 days", "project", "m1", "30 days", .8)


def capture():
    return {"scope": "project:test", "provider": "codex", "session_hash": "session",
            "messages": [
                {"message_id": "m1", "role": "assistant", "text": "The limit is 30 days.",
                 "timestamp": "2026-09-01T10:00:00Z"},
                {"message_id": "m2", "role": "user", "text": "Change the limit to 3650 days.",
                 "timestamp": "2026-09-01T10:05:00Z"}]}


def accepted(bound):
    return {"verdict": "accept", "source_hash": bound.source_context["source_hash"],
            "checks": dict.fromkeys(CHECKS, True)}


def ingest(service):
    return service.ingest(candidate().text, [CitationInput("dream-worker", "dream:codex:session:m1", "30 days")],
                          scope="project:test", source_agent="dream-worker", claim_type="fact",
                          subject="importer", predicate="limit", object_value="30 days", confidence=.9)


def test_source_includes_later_corrections_and_original_scope():
    bound = bind_source(candidate(), capture())
    context = bound.to_dict()["source_context"]
    assert context["capture_scope"] == "project:test"
    assert "3650 days" in context["messages"][1]["text"]
    assert context["complete"] is True
    changed = capture()
    changed["messages"][1]["text"] = "The limit remains 30 days."
    assert bind_source(candidate(), changed).source_context["source_hash"] != context["source_hash"]


@pytest.mark.parametrize("check", CHECKS)
def test_accept_requires_every_source_check(check):
    bound = bind_source(candidate(), capture())
    payload = accepted(bound)
    payload["checks"][check] = False
    with pytest.raises(ValueError, match="review"):
        parse_review(payload, bound)


def test_incomplete_or_wrong_source_cannot_be_accepted():
    bound = bind_source(candidate(), capture(), max_chars=10)
    with pytest.raises(ValueError, match="review"):
        parse_review(accepted(bound), bound)
    bound = bind_source(candidate(), capture())
    payload = {**accepted(bound), "source_hash": "wrong"}
    with pytest.raises(ValueError, match="review"):
        parse_review(payload, bound)


def test_context_redacts_numeric_credentials_and_home_paths():
    row = capture()
    row["messages"][0]["text"] += " WiFi 9876543210; ~/private/settings.ini"
    context = str(bind_source(candidate(), row).to_dict())
    assert "9876543210" not in context
    assert "~/private" not in context


def test_missing_review_blocks_both_promotion_routes(service):
    claim = ingest(service)
    stats = validator.run(service.store, min_score=0)
    assert stats["pending"] == 1
    assert service.store.get_claim(claim.id).status == "candidate"
    with pytest.raises(ValueError, match="source.review"):
        service.store.apply_status_transition(claim, to_status="confirmed", reason="test", event_type="validator")
    with service.store.connect() as conn:
        assert not _confirm_candidate_cas(conn, claim_id=claim.id, version=claim.version,
                                          subject="importer", predicate="limit", object_value="30 days", confidence=.9)


def test_review_receipt_binds_claim_and_citations_and_is_replay_safe(service):
    claim = ingest(service)
    bound = bind_source(candidate(), capture())
    review = parse_review(accepted(bound), bound)
    record_review(service.store, claim.id, bound, review)
    record_review(service.store, claim.id, bound, review)
    assert review_allows_confirmation(service.store, claim.id)
    events = service.list_events(claim_id=claim.id, event_type="audit", limit=100)
    assert sum(e.details == "dream_source_review_v1" for e in events) == 1
    with service.store.connect() as conn:
        conn.execute("UPDATE citations SET excerpt = 'different evidence' WHERE claim_id = ?", (claim.id,))
        conn.commit()
    assert not review_allows_confirmation(service.store, claim.id)


def test_reviewed_candidate_can_pass_normal_steward(service):
    claim = ingest(service)
    bound = bind_source(candidate(), capture())
    record_review(service.store, claim.id, bound, parse_review(accepted(bound), bound))
    validator.run(service.store, min_score=0)
    assert service.store.get_claim(claim.id).status == "confirmed"


def test_sanitized_or_rewritten_candidate_cannot_borrow_review(service):
    claim = ingest(service)
    bound = bind_source(replace(candidate(), text="A different claim entirely."), capture())
    with pytest.raises(ValueError, match="review"):
        record_review(service.store, claim.id, bound, parse_review(accepted(bound), bound))


@pytest.mark.parametrize("field,value", [
    ("text", "The importer limit is 3650 days."), ("scope", "personal"),
    ("tenant_id", "different-tenant"), ("valid_until", "2026-09-02T10:00:00Z"),
    ("visibility", "sensitive"), ("object_value", "3650 days"),
])
def test_any_reviewed_claim_change_blocks_promotion(service, field, value):
    claim = ingest(service)
    bound = bind_source(candidate(), capture())
    record_review(service.store, claim.id, bound, parse_review(accepted(bound), bound))
    with service.store.connect() as conn:
        conn.execute(f"UPDATE claims SET {field} = ? WHERE id = ?", (value, claim.id))
        conn.commit()
    assert not review_allows_confirmation(service.store, claim.id)
    with pytest.raises(ValueError, match="source review"):
        service.store.apply_status_transition(service.store.get_claim(claim.id), to_status="confirmed",
                                              reason="test", event_type="validator")


@pytest.mark.parametrize("verdict", ["reject", "needs_evidence"])
def test_negative_review_remains_candidate(service, verdict):
    claim = ingest(service)
    bound = bind_source(candidate(), capture())
    record_review(service.store, claim.id, bound, {**accepted(bound), "verdict": verdict})
    assert validator.run(service.store, min_score=0)["pending"] == 1
    assert service.store.get_claim(claim.id).status == "candidate"


def test_real_provider_parser_requires_review_and_discards_extra_review_fields():
    bound = bind_source(candidate(), capture())
    decision = {"candidate_id": bound.candidate_id, "action": "add", "confidence": .8, "rationale": "test"}
    kwargs = dict(started=time.monotonic(), input_tokens=1, output_tokens=1, provider="antigravity", model="gemini-test")
    with pytest.raises(ValueError, match="review"):
        consolidation_from_raw(json.dumps({"decisions": [decision]}), [bound], **kwargs)
    decision["source_review"] = {**accepted(bound), "unwanted_raw_context": "must not persist"}
    result = consolidation_from_raw(json.dumps({"decisions": [decision]}), [bound], **kwargs)
    assert result.decisions[0].source_review == accepted(bound)


def test_prompt_covers_observed_failure_modes_without_extra_model_call():
    bound = bind_source(candidate(), capture())
    prompt = consolidation_prompt([bound], [], "project:test")
    for instruction in ("later corrections", "question or proposal", "preserve doubts",
                        "Project-only", "Vague subjects", "untrusted evidence"):
        assert instruction in prompt
    data = json.loads(prompt.split("INPUT:\n", 1)[1])
    assert data["candidates"][0]["source_context"]["messages"][1]["text"] == capture()["messages"][1]["text"]


def test_postgres_dream_promotion_fails_closed(service):
    from memorymaster.stores.postgres_store import PostgresStore

    claim = ingest(service)
    with pytest.raises(ValueError, match="SQLite-only"):
        PostgresStore.apply_status_transition(object(), claim, to_status="confirmed", reason="test", event_type="validator")


@pytest.mark.parametrize("omit_review", [False, True])
def test_worker_uses_one_existing_gemini_call_and_persists_review(tmp_path, service, omit_review):
    from memorymaster.dreaming.ledger import DreamLedger
    from memorymaster.dreaming.models import CaptureEnvelope, DreamMessage, ExtractionResult, ProviderUsage
    from memorymaster.dreaming.providers import AntigravityConsolidator
    from memorymaster.dreaming.worker import DreamConfig, DreamWorker

    ledger = DreamLedger(tmp_path / "capture.db")
    row = capture()
    capture_id = ledger.enqueue(CaptureEnvelope(
        provider="codex", session_hash="session", scope=row["scope"],
        captured_at="2026-09-01T11:00:00Z", last_activity_at="2026-09-01T10:05:00Z",
        messages=tuple(DreamMessage(**m) for m in row["messages"]),
        cursor_start=0, cursor_end=100, content_hash="fixture-hash"))
    calls = []

    class Extractor:
        provider, model = "google", "gemini-test"

        def extract(self, messages, **kwargs):
            return ExtractionResult((candidate(),), ProviderUsage("google", "gemini-test", 200, 1, 1, 1, True))

    def complete(prompt):
        calls.append(prompt)
        supplied = json.loads(prompt.split("INPUT:\n", 1)[1])["candidates"][0]
        assert supplied["source_context"]["messages"][1]["text"] == row["messages"][1]["text"]
        decision = {"candidate_id": candidate().candidate_id, "action": "add", "confidence": .8}
        if not omit_review:
            # Synthetic verdict tests orchestration, not a claim of model precision.
            decision["source_review"] = {"verdict": "needs_evidence",
                "source_hash": supplied["source_context"]["source_hash"], "checks": dict.fromkeys(CHECKS, False)}
        return SimpleNamespace(text=json.dumps({"decisions": [decision]}), input_tokens=1, output_tokens=1)

    worker = DreamWorker(ledger, service, Extractor(),
        AntigravityConsolidator(model="gemini-test", client=SimpleNamespace(complete=complete)),
        config=DreamConfig(), now=lambda: datetime(2026, 9, 1, 12, tzinfo=timezone.utc))
    result = worker.run(apply_candidates=True)
    assert len(calls) == 1
    if omit_review:
        assert result["errors"] == 1
        assert ledger.get_capture(capture_id)["state"] == "retryable"
        assert service.list_claims(limit=10) == []
    else:
        assert result["errors"] == 0
        claims = service.list_claims(limit=10)
        assert len(claims) == 1 and claims[0].status == "candidate"
        assert not review_allows_confirmation(service.store, claims[0].id)
        ledger.mark_retryable(capture_id, "crash-fixture", "interrupted after application")
        worker.run(apply_candidates=True)
        assert len(calls) == 1
        assert len(service.list_claims(limit=10)) == 1
        assert len(ledger.get_capture(capture_id)["decisions"]) == 1
