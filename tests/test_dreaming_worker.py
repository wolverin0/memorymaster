from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.models import (
    CaptureEnvelope,
    ConsolidationResult,
    DreamCandidate,
    DreamDecision,
    DreamMessage,
    ExtractionResult,
    ProviderUsage,
)
from memorymaster.dreaming.providers import ProviderCallError
from memorymaster.dreaming.worker import DreamConfig, DreamWorker


NOW = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)


def _usage(provider: str, model: str) -> ProviderUsage:
    return ProviderUsage(provider, model, 200, 10, 20, 5, True)


def _capture(
    ledger: DreamLedger,
    *,
    scope: str = "project:test",
    session_hash: str = "session",
) -> int:
    messages = (
        DreamMessage("m1", "user", "I prefer blue interfaces for daily work.", (NOW - timedelta(hours=1)).isoformat()),
        DreamMessage("m2", "assistant", "Blue interfaces will be treated as your preference.", (NOW - timedelta(minutes=59)).isoformat()),
    )
    return ledger.enqueue(CaptureEnvelope(
        provider="codex", session_hash=session_hash, scope=scope,
        captured_at=NOW.isoformat(), last_activity_at=messages[-1].timestamp,
        messages=messages, cursor_start=0, cursor_end=100, content_hash="capture-hash",
    ))


class _Extractor:
    model = "gemini-3.5-flash"
    provider = "google"

    def extract(self, messages, *, scope, capture_hash):
        return ExtractionResult((
            DreamCandidate("project-c", "The blue interface is selected for this project.", "decision", "interface", "uses", "blue", "project", "m2", "Blue interfaces", 0.8),
            DreamCandidate("personal-c", "The user prefers blue interfaces.", "preference", "user", "prefers", "blue interfaces", "personal", "m1", "prefer blue interfaces", 0.9),
        ), _usage("google", self.model))


class _Consolidator:
    model = "glm-5.2"
    provider = "zai"

    def __init__(self) -> None:
        self.scopes: list[tuple[str, set[str]]] = []

    def consolidate(self, candidates, current_claims, *, scope):
        self.scopes.append((scope, {str(claim["scope"]) for claim in current_claims}))
        return ConsolidationResult(
            tuple(DreamDecision(candidate.candidate_id, "add", "new", 0.9) for candidate in candidates),
            _usage("zai", self.model),
        )


class _NoCall:
    model = "unused"
    provider = "unused"

    def __getattr__(self, name):
        raise AssertionError(f"provider should not be called: {name}")


def _service(tmp_path: Path) -> MemoryService:
    service = MemoryService(tmp_path / "claims.db", workspace_root=tmp_path)
    service.init_db()
    return service


def test_shadow_then_apply_is_candidate_first_scope_safe_and_replay_safe(tmp_path: Path) -> None:
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    _capture(ledger)
    other = service.ingest(
        "Another project uses a red interface.", [CitationInput("test")],
        scope="project:other", source_agent="test", claim_type="decision",
    )
    service.store.apply_status_transition(other, to_status="confirmed", reason="test", event_type="validator")
    consolidator = _Consolidator()
    config = DreamConfig(idle_minutes=30, max_sessions=20)

    shadow = DreamWorker(ledger, service, _Extractor(), consolidator, config=config, now=lambda: NOW).run(apply_candidates=False)

    assert shadow["consolidated"] == 1
    assert service.list_claims(status="candidate", limit=20, scope_allowlist=["project:test", "personal"]) == []
    assert consolidator.scopes == [("personal", set()), ("project:test", set())]

    applied = DreamWorker(ledger, service, _NoCall(), _NoCall(), config=config, now=lambda: NOW).run(apply_candidates=True)
    claims = service.list_claims(status="candidate", limit=20, scope_allowlist=["project:test", "personal"])

    assert applied["applied"] == 1
    assert {(claim.scope, claim.text) for claim in claims} == {
        ("project:test", "The blue interface is selected for this project."),
        ("personal", "The user prefers blue interfaces."),
    }
    replay = DreamWorker(ledger, service, _NoCall(), _NoCall(), config=config, now=lambda: NOW).run(apply_candidates=True)
    assert replay["candidate_writes"] == 0
    assert len(service.list_claims(status="candidate", limit=20, scope_allowlist=["project:test", "personal"])) == 2


def test_provider_failure_is_replayable_and_never_mutates_claims(tmp_path: Path) -> None:
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)

    class Broken(_Extractor):
        def extract(self, messages, *, scope, capture_hash):
            raise RuntimeError("provider unavailable")

    result = DreamWorker(ledger, service, Broken(), _Consolidator(), config=DreamConfig(), now=lambda: NOW).run(apply_candidates=True)

    assert result["errors"] == 1
    assert ledger.get_capture(capture_id)["state"] == "retryable"
    assert service.list_claims(limit=20, scope_allowlist=["project:test"]) == []


def test_extraction_rate_limit_opens_batch_circuit_and_records_429(tmp_path: Path) -> None:
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    first_id = _capture(ledger, session_hash="first")
    second_id = _capture(ledger, session_hash="second")

    class RateLimited(_Extractor):
        calls = 0

        def extract(self, messages, *, scope, capture_hash):
            self.calls += 1
            raise ProviderCallError("provider request failed with HTTP 429", http_status=429)

    extractor = RateLimited()
    result = DreamWorker(
        ledger, service, extractor, _Consolidator(), config=DreamConfig(), now=lambda: NOW,
    ).run(apply_candidates=False)

    assert result["errors"] == 1
    assert extractor.calls == 1
    assert ledger.get_capture(first_id)["state"] == "retryable"
    assert ledger.get_capture(second_id)["state"] == "captured"
    assert DreamLedger.read_status(ledger.db_path)["providers"]["google"]["http_429"] == 1


def test_repeated_semantic_extraction_failure_is_quarantined(tmp_path: Path) -> None:
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)

    class InvalidEvidence(_Extractor):
        def extract(self, messages, *, scope, capture_hash):
            raise ValueError("evidence quote is not exact")

    worker = DreamWorker(
        ledger,
        service,
        InvalidEvidence(),
        _Consolidator(),
        config=DreamConfig(max_semantic_attempts=2),
        now=lambda: NOW,
    )
    worker.run(apply_candidates=False)
    worker.run(apply_candidates=False)

    assert ledger.get_capture(capture_id)["state"] == "quarantined"


def test_consolidation_retry_reuses_durable_extraction(tmp_path: Path) -> None:
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)

    class BrokenConsolidator(_Consolidator):
        def consolidate(self, candidates, current_claims, *, scope):
            raise RuntimeError("temporary consolidation failure")

    first = DreamWorker(
        ledger,
        service,
        _Extractor(),
        BrokenConsolidator(),
        config=DreamConfig(),
        now=lambda: NOW,
    ).run(apply_candidates=False)

    assert first["extracted"] == 1
    assert ledger.get_capture(capture_id)["state"] == "retryable"

    second = DreamWorker(
        ledger,
        service,
        _NoCall(),
        _Consolidator(),
        config=DreamConfig(),
        now=lambda: NOW,
    ).run(apply_candidates=False)

    assert second["consolidated"] == 1
    assert ledger.get_capture(capture_id)["state"] == "consolidated"


def test_application_retry_reuses_durable_decisions(tmp_path: Path) -> None:
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    blocked = DreamConfig(max_candidate_writes_daily=0)

    first = DreamWorker(
        ledger, service, _Extractor(), _Consolidator(), config=blocked, now=lambda: NOW
    ).run(apply_candidates=True)

    assert first["errors"] == 1
    assert ledger.get_capture(capture_id)["decisions"] is not None
    assert ledger.get_capture(capture_id)["state"] == "retryable"

    second = DreamWorker(
        ledger, service, _NoCall(), _NoCall(), config=DreamConfig(), now=lambda: NOW
    ).run(apply_candidates=True)

    assert second["applied"] == 1
    assert ledger.get_capture(capture_id)["state"] == "applied"


def test_ignore_decision_does_not_consume_candidate_write_budget(tmp_path: Path) -> None:
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)

    class IgnoreConsolidator(_Consolidator):
        def consolidate(self, candidates, current_claims, *, scope):
            return ConsolidationResult(
                tuple(DreamDecision(candidate.candidate_id, "ignore", "ephemeral", 0.9) for candidate in candidates),
                _usage("zai", self.model),
            )

    result = DreamWorker(
        ledger,
        service,
        _Extractor(),
        IgnoreConsolidator(),
        config=DreamConfig(max_candidate_writes_daily=0),
        now=lambda: NOW,
    ).run(apply_candidates=True)

    assert result["applied"] == 1
    assert result["candidate_writes"] == 0
    assert ledger.get_capture(capture_id)["state"] == "applied"


def test_lifecycle_recommendation_emits_proposal_without_transition(tmp_path: Path) -> None:
    service = _service(tmp_path)
    old = service.ingest(
        "The interface preference is green.", [CitationInput("test")],
        scope="project:test", source_agent="test", claim_type="preference",
    )
    old = service.store.apply_status_transition(old, to_status="confirmed", reason="test", event_type="validator")
    ledger = DreamLedger(tmp_path / "capture.db")
    _capture(ledger)

    class OneExtractor(_Extractor):
        def extract(self, messages, *, scope, capture_hash):
            candidate = DreamCandidate("replace-c", "The interface preference is now blue.", "preference", "interface", "prefers", "blue", "project", "m1", "prefer blue interfaces", 0.9)
            return ExtractionResult((candidate,), _usage("google", self.model))

    class Proposer(_Consolidator):
        def consolidate(self, candidates, current_claims, *, scope):
            decision = DreamDecision("replace-c", "propose_supersede", "newer evidence", 0.95, old.id)
            return ConsolidationResult((decision,), _usage("zai", self.model))

    DreamWorker(ledger, service, OneExtractor(), Proposer(), config=DreamConfig(), now=lambda: NOW).run(apply_candidates=True)

    assert service.store.get_claim(old.id).status == "confirmed"
    events = service.list_events(claim_id=old.id, event_type="policy_decision", limit=20)
    assert any(event.details == "steward_proposal:superseded_candidate" for event in events)
