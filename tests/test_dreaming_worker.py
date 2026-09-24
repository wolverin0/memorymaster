from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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


def _accepted_review(candidate: DreamCandidate, scope: str) -> dict:
    """Build a synthetic v2 acceptance receipt for orchestration tests only."""
    kind = candidate.claim_type if candidate.claim_type in {
        "lesson", "decision", "preference", "constraint", "profile",
    } else "lesson"
    key_parts = [scope, candidate.subject, candidate.predicate, candidate.object_value or "value"]
    memory_key = ".".join(
        "_".join(str(part).lower().split()) for part in key_parts
    )[:160]
    return {
        "version": 2,
        # This is an explicit synthetic verdict: it exercises worker plumbing,
        # not provider precision or a human quality judgment.
        "verdict": "accept",
        "source_hash": candidate.source_context["source_hash"],
        "checks": {
            "evidence": True,
            "chronology": True,
            "modality": True,
            "scope": True,
            "specificity": True,
            "privacy": True,
            "usefulness": True,
            "novelty": True,
        },
        "selection": {
            "destination": "memory",
            "kind": kind,
            "novelty": "new",
            "scope": scope,
            "memory_key": memory_key,
            "future_use": "Use this stable rule in future consolidation decisions.",
        },
    }


def _capture(
    ledger: DreamLedger,
    *,
    scope: str = "project:test",
    session_hash: str = "session",
    extra_text: str = "",
) -> int:
    messages = (
        DreamMessage("m1", "user", "I prefer blue interfaces for daily work.", (NOW - timedelta(hours=1)).isoformat()),
        DreamMessage("m2", "assistant", "Blue interfaces will be treated as your preference." + extra_text, (NOW - timedelta(minutes=59)).isoformat()),
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
            tuple(
                DreamDecision(
                    candidate.candidate_id,
                    "add",
                    "synthetic accepted selection",
                    0.9,
                    source_review=_accepted_review(candidate, scope),
                )
                for candidate in candidates
            ),
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


def test_extraction_budget_defers_without_retry_or_error_churn(tmp_path: Path) -> None:
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)

    result = DreamWorker(
        ledger,
        service,
        _NoCall(),
        _NoCall(),
        config=DreamConfig(max_extract_calls_daily=0),
        now=lambda: NOW,
    ).run(apply_candidates=False)

    capture = ledger.get_capture(capture_id)
    assert result["errors"] == 0
    assert result["deferred_extract_budget"] == 1
    assert capture["state"] == "captured"
    assert capture["attempts"] == 0


def test_same_provider_models_keep_stage_budgets_independent(tmp_path: Path) -> None:
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)

    class OpenAIExtractor(_Extractor):
        provider = "openai"
        model = "openai/gpt-5.4-mini"

    class OpenAIConsolidator(_Consolidator):
        provider = "openai"
        model = "openai/gpt-5.6-luna"

    result = DreamWorker(
        ledger,
        service,
        OpenAIExtractor(),
        OpenAIConsolidator(),
        config=DreamConfig(max_consolidate_calls_daily=1),
        now=lambda: NOW,
    ).run(apply_candidates=False)

    assert result["errors"] == 0
    assert result["extracted"] == 1
    assert result["consolidated"] == 1
    assert ledger.get_capture(capture_id)["state"] == "consolidated"


def test_consolidation_budget_defers_without_retry_or_error_churn(tmp_path: Path) -> None:
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)

    result = DreamWorker(
        ledger,
        service,
        _Extractor(),
        _Consolidator(),
        config=DreamConfig(max_consolidate_calls_daily=0),
        now=lambda: NOW,
    ).run(apply_candidates=False)

    capture = ledger.get_capture(capture_id)
    assert result["errors"] == 0
    assert result["deferred_consolidate_budget"] == 1
    assert capture["state"] == "extracted"
    assert capture["attempts"] == 1
    assert capture["last_error"] is None


def test_consolidation_batches_bound_candidate_ids_without_splitting_capture(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    for index in range(3):
        _capture(ledger, session_hash=f"batch-{index}")

    class ManyExtractor(_Extractor):
        def extract(self, messages, *, scope, capture_hash):
            candidates = tuple(
                DreamCandidate(
                    f"{capture_hash[:8]}-{index}",
                    f"Stable fact {index}.",
                    "fact",
                    "project",
                    "records",
                    str(index),
                    "project",
                    "m1",
                    "prefer blue interfaces",
                    0.9,
                )
                for index in range(5)
            )
            return ExtractionResult(candidates, _usage("openai", self.model))

    class BoundedConsolidator(_Consolidator):
        batch_sizes: list[int] = []

        def consolidate(self, candidates, current_claims, *, scope):
            del current_claims, scope
            self.batch_sizes.append(len(candidates))
            decisions = tuple(
                DreamDecision(candidate.candidate_id, "ignore", "test", 0.9)
                for candidate in candidates
            )
            return ConsolidationResult(decisions, _usage("openai", self.model))

    consolidator = BoundedConsolidator()
    result = DreamWorker(
        ledger,
        service,
        ManyExtractor(),
        consolidator,
        config=DreamConfig(max_consolidate_candidates=5),
        now=lambda: NOW,
    ).run(apply_candidates=False)

    assert result["errors"] == 0
    assert result["consolidated"] == 3
    assert consolidator.batch_sizes == [5, 5, 5]


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


def test_repeated_source_review_validation_failure_is_quarantined(tmp_path: Path) -> None:
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)

    class InvalidSourceReview(_Consolidator):
        def consolidate(self, candidates, current_claims, *, scope):
            del current_claims
            decisions = []
            for candidate in candidates:
                review = _accepted_review(candidate, scope)
                review["source_hash"] = "wrong-source-hash"
                decisions.append(
                    DreamDecision(
                        candidate.candidate_id,
                        "add",
                        "invalid source review",
                        0.9,
                        source_review=review,
                    )
                )
            return ConsolidationResult(tuple(decisions), _usage("zai", self.model))

    worker = DreamWorker(
        ledger,
        service,
        _Extractor(),
        InvalidSourceReview(),
        config=DreamConfig(max_semantic_attempts=2),
        now=lambda: NOW,
    )
    worker.run(apply_candidates=False)
    worker.run(apply_candidates=False)

    capture = ledger.get_capture(capture_id)
    assert capture["state"] == "quarantined"
    assert "source review fingerprint mismatch" in capture["last_error"]


def test_durable_extraction_counts_failures_not_successful_transitions(tmp_path):
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    candidate = _Extractor().extract([], scope="project:test", capture_hash="a").candidates[0]
    ledger.set_extraction(capture_id, [candidate.to_dict()], "fixture")

    class Invalid(_Consolidator):
        def consolidate(self, candidates, current_claims, *, scope):
            raise ValueError("invalid semantic output")

    worker = DreamWorker(ledger, service, _NoCall(), Invalid(),
                         config=DreamConfig(max_semantic_attempts=2), now=lambda: NOW)
    worker.run(apply_candidates=False)
    assert ledger.get_capture(capture_id)["state"] == "retryable"
    worker.run(apply_candidates=False)
    assert ledger.get_capture(capture_id)["state"] == "quarantined"


def test_application_retry_counter_survives_cached_decision_replay(tmp_path, monkeypatch):
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    worker = DreamWorker(ledger, service, _Extractor(), _Consolidator(),
                         config=DreamConfig(max_semantic_attempts=2), now=lambda: NOW)
    def reject(*args):
        raise ValueError("proposal target no longer authorized")
    monkeypatch.setattr(worker, "_apply_capture", reject)
    worker.run(apply_candidates=True)
    assert ledger.get_capture(capture_id)["state"] == "retryable"
    worker.run(apply_candidates=True)
    assert ledger.get_capture(capture_id)["state"] == "quarantined"
    assert service.list_claims(limit=20) == []


def test_transient_consolidation_error_preserves_semantic_count_without_advancing_it(tmp_path):
    # Contract changed in 4.9.0 (review F-05). This test used to be
    # test_transient_consolidation_error_does_not_exhaust_semantic_retries and
    # locked in a RESET: V, T, V, V quarantined only on the fourth run, so an
    # alternating provider could keep a bad capture paying forever. Now a
    # transient error neither advances nor resets the semantic count.
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    candidate = _Extractor().extract([], scope="project:test", capture_hash="a").candidates[0]
    ledger.set_extraction(capture_id, [candidate.to_dict()], "fixture")

    class Flaky(_Consolidator):
        errors = iter([ValueError("invalid"), RuntimeError("timeout"), ValueError("invalid")])
        def consolidate(self, candidates, current_claims, *, scope):
            raise next(self.errors)

    worker = DreamWorker(ledger, service, _NoCall(), Flaky(),
                         config=DreamConfig(max_semantic_attempts=2), now=lambda: NOW)
    worker.run(apply_candidates=False)
    capture = ledger.get_capture(capture_id)
    assert capture["state"] == "retryable"
    assert capture["last_error"].startswith("semantic-failure-v1:consolidate:1:")
    worker.run(apply_candidates=False)
    capture = ledger.get_capture(capture_id)
    assert capture["state"] == "retryable"
    assert capture["last_error"] == "semantic-failure-v1:consolidate:1:timeout"
    worker.run(apply_candidates=False)
    capture = ledger.get_capture(capture_id)
    assert capture["state"] == "quarantined"
    assert capture["last_error"].startswith("semantic-failure-v1:consolidate:2:")


def _extracted_capture(ledger: DreamLedger, *, session_hash: str = "session", candidate_id: str | None = None,
                       scope: str = "project:test", extra_text: str = "") -> int:
    from dataclasses import replace

    capture_id = _capture(ledger, scope=scope, session_hash=session_hash, extra_text=extra_text)
    candidate = _Extractor().extract([], scope="project:test", capture_hash="a").candidates[0]
    if candidate_id is not None:
        candidate = replace(candidate, candidate_id=candidate_id)
    ledger.set_extraction(capture_id, [candidate.to_dict()], "fixture")
    return capture_id


def test_alternating_semantic_and_transient_errors_still_reach_quarantine(tmp_path):
    # Review F-05 / repro r2 (A): ValueError and ProviderCallError alternated,
    # and each transient error wiped the semantic count.
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _extracted_capture(ledger)

    class Alternating(_Consolidator):
        calls = 0

        def consolidate(self, candidates, current_claims, *, scope):
            type(self).calls += 1
            if type(self).calls % 2:
                raise ValueError("decision references an unknown candidate")
            raise ProviderCallError("provider request failed with HTTP 503", http_status=503)

    worker = DreamWorker(ledger, service, _NoCall(), Alternating(),
                         config=DreamConfig(max_semantic_attempts=2), now=lambda: NOW)
    for _ in range(4):
        worker.run(apply_candidates=False)
    assert ledger.get_capture(capture_id)["state"] == "quarantined"
    assert Alternating.calls == 3


def test_budget_deferral_preserves_semantic_failure_count(tmp_path):
    # Review F-05 / repro r2 (B): defer_consolidation cleared last_error, so
    # fail -> defer -> fail stayed at consolidate:1 forever.
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _extracted_capture(ledger)

    class Bad(_Consolidator):
        calls = 0

        def consolidate(self, candidates, current_claims, *, scope):
            type(self).calls += 1
            raise ValueError("source review fingerprint mismatch")

    failing = DreamWorker(ledger, service, _NoCall(), Bad(),
                          config=DreamConfig(max_semantic_attempts=2), now=lambda: NOW)
    capped = DreamWorker(ledger, service, _NoCall(), Bad(),
                         config=DreamConfig(max_semantic_attempts=2, max_consolidate_calls_daily=0),
                         now=lambda: NOW)
    failing.run(apply_candidates=False)
    capped.run(apply_candidates=False)
    deferred = ledger.get_capture(capture_id)
    assert deferred["state"] == "extracted"
    assert deferred["deferred_reason"] == "consolidate_budget"
    assert deferred["last_error"].startswith("semantic-failure-v1:consolidate:1:")
    failing.run(apply_candidates=False)
    assert ledger.get_capture(capture_id)["state"] == "quarantined"
    assert Bad.calls == 2


def test_absolute_capture_error_cap_quarantines_persistent_unexpected_failures(tmp_path, monkeypatch):
    # Transient/provider-wide failures no longer count (operator, 2026-09-23);
    # an unexpected error with no provider-wide cause still does.
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _extracted_capture(ledger)

    class Unexpected(_Consolidator):
        calls = 0

        def consolidate(self, candidates, current_claims, *, scope):
            type(self).calls += 1
            raise RuntimeError("unexpected consolidator state")

    worker = DreamWorker(ledger, service, _NoCall(), Unexpected(),
                         config=DreamConfig(max_capture_errors=3), now=lambda: NOW)
    for expected in (1, 2):
        worker.run(apply_candidates=False)
        capture = ledger.get_capture(capture_id)
        assert (capture["state"], capture["error_count"]) == ("retryable", expected)
    worker.run(apply_candidates=False)
    worker.run(apply_candidates=False)
    capture = ledger.get_capture(capture_id)
    assert (capture["state"], capture["error_count"]) == ("quarantined", 3)
    assert Unexpected.calls == 3
    assert DreamConfig().max_capture_errors == 8
    monkeypatch.setenv("MEMORYMASTER_DREAM_MAX_CAPTURE_ERRORS", "5")
    assert DreamConfig.from_env().max_capture_errors == 5


def test_capture_error_cap_is_absolute_across_stage_successes(tmp_path):
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)

    class FailsOnce(_Extractor):
        calls = 0

        def extract(self, messages, *, scope, capture_hash):
            type(self).calls += 1
            if type(self).calls == 1:
                raise RuntimeError("unexpected extractor state")
            return super().extract(messages, scope=scope, capture_hash=capture_hash)

    class Unexpected(_Consolidator):
        def consolidate(self, candidates, current_claims, *, scope):
            raise RuntimeError("unexpected consolidator state")

    worker = DreamWorker(ledger, service, FailsOnce(), Unexpected(),
                         config=DreamConfig(max_capture_errors=2), now=lambda: NOW)
    worker.run(apply_candidates=False)
    assert ledger.get_capture(capture_id)["error_count"] == 1
    # Extraction succeeds (stage success resets the semantic count) but the
    # absolute per-capture count keeps the earlier failure.
    worker.run(apply_candidates=False)
    capture = ledger.get_capture(capture_id)
    assert (capture["state"], capture["error_count"]) == ("quarantined", 2)


def test_budget_deferrals_do_not_count_toward_capture_error_cap(tmp_path):
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    blocked = DreamConfig(max_candidate_writes_daily=0, max_capture_errors=2)
    worker = DreamWorker(ledger, service, _Extractor(), _Consolidator(), config=blocked, now=lambda: NOW)
    for _ in range(3):
        worker.run(apply_candidates=True)
        capture = ledger.get_capture(capture_id)
        assert (capture["state"], capture["error_count"]) == ("retryable", 0)
        assert capture["last_error"] == "candidate_write_daily_budget_exhausted"
    applied = DreamWorker(ledger, service, _NoCall(), _NoCall(), config=DreamConfig(),
                          now=lambda: NOW).run(apply_candidates=True)
    assert applied["applied"] == 1
    assert ledger.get_capture(capture_id)["state"] == "applied"


def _gemini_extractor(transport, calls: list):
    from memorymaster.dreaming.providers import GeminiExtractor

    def counted(url, payload, headers, timeout):
        calls.append(url)
        return transport(url, payload, headers, timeout)

    return GeminiExtractor(api_key="fixture-key", model="gemini-test", transport=counted,
                           sleep=lambda _seconds: None)


def _http_status(status: int):
    return lambda *_args: (status, {"error": {"message": "fixture"}}, {})


def _transport_raises(error: Exception):
    def transport(*_args):
        raise error
    return transport


def _agy_consolidator(tmp_path: Path, runner, calls: list):
    import sys

    from memorymaster.core.antigravity_client import AntigravityClient
    from memorymaster.dreaming.providers import AntigravityConsolidator

    def counted(command, prompt, timeout, cwd, env):
        calls.append(prompt)
        return runner(command, prompt, timeout, cwd, env)

    client = AntigravityClient(model="gemini-test", command=sys.executable, runner=counted,
                               work_dir=tmp_path / "agy")
    return AntigravityConsolidator(model="gemini-test", client=client)


def _agy_timeout(command, prompt, timeout, cwd, env):
    import subprocess

    raise subprocess.TimeoutExpired(command, timeout)


def _agy_quota(command, prompt, timeout, cwd, env):
    import json
    import subprocess

    event = {"event": "result", "result": {"status": "QUOTA_EXHAUSTED", "error": "quota resets at 00:00"}}
    return subprocess.CompletedProcess(command, 1, json.dumps(event), "")


def _extraction_failure(status_or_error):
    def build(tmp_path, calls):
        transport = (_http_status(status_or_error) if isinstance(status_or_error, int)
                     else _transport_raises(status_or_error))
        return _gemini_extractor(transport, calls), _NoCall(), False
    return build


def _consolidation_failure(runner):
    def build(tmp_path, calls):
        return _NoCall(), _agy_consolidator(tmp_path, runner, calls), True
    return build


def test_five_rate_limited_runs_never_consume_the_capture_error_cap(tmp_path):
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    calls: list = []
    worker = DreamWorker(ledger, service, _gemini_extractor(_http_status(429), calls), _NoCall(),
                         config=DreamConfig(max_capture_errors=2), now=lambda: NOW)
    for run in range(1, 6):
        worker.run(apply_candidates=False)
        capture = ledger.get_capture(capture_id)
        assert (capture["state"], capture["error_count"]) == ("retryable", 0), run
    assert len(calls) == 5 * 4  # every run really reached the provider (4 HTTP attempts each)


@pytest.mark.parametrize("build", [
    pytest.param(_extraction_failure(500), id="extract-http-500"),
    pytest.param(_extraction_failure(503), id="extract-http-503"),
    pytest.param(_extraction_failure(529), id="extract-http-529-overloaded"),
    pytest.param(_extraction_failure(TimeoutError("timed out")), id="extract-timeout"),
    pytest.param(_extraction_failure(ConnectionResetError("reset by peer")), id="extract-connection-reset"),
    pytest.param(_consolidation_failure(_agy_timeout), id="consolidate-antigravity-timeout"),
    pytest.param(_consolidation_failure(_agy_quota), id="consolidate-antigravity-quota"),
])
def test_five_server_error_or_timeout_runs_never_consume_the_capture_error_cap(tmp_path, build):
    # Operator decision 2026-09-23: the absolute per-capture cap counts only
    # semantic and unexpected errors. A provider outage says nothing about the
    # capture; with cap=2 the old code quarantined it on the second run.
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    calls: list = []
    extractor, consolidator, pre_extracted = build(tmp_path, calls)
    capture_id = _extracted_capture(ledger) if pre_extracted else _capture(ledger)
    worker = DreamWorker(ledger, service, extractor, consolidator,
                         config=DreamConfig(max_capture_errors=2), now=lambda: NOW)
    for run in range(1, 6):
        worker.run(apply_candidates=False)
        capture = ledger.get_capture(capture_id)
        assert (capture["state"], capture["error_count"]) == ("retryable", 0), run
        assert calls, "the provider must actually have been called"
    assert len(calls) >= 5


def test_semantic_and_unexpected_errors_still_count_against_the_capture():
    import json

    from memorymaster.dreaming.providers import ProviderOutputError
    from memorymaster.dreaming.worker import _is_deferral

    def wrapped(cause):
        try:
            raise ProviderCallError("provider request failed") from cause
        except ProviderCallError as error:
            return error

    counted = [
        ValueError("decision references an unknown candidate"),
        ProviderOutputError("provider returned malformed JSON"),
        ProviderCallError("provider request failed with HTTP 400", http_status=400),
        wrapped(json.JSONDecodeError("Expecting value", "<html>", 0)),
        RuntimeError("unexpected"),
        KeyError("candidate"),
    ]
    assert [_is_deferral(error) for error in counted] == [False] * len(counted)


def test_failed_call_tokens_count_against_daily_token_budget(tmp_path):
    # Review F-04 (second half) / repro r1: failed calls recorded input_tokens=0,
    # so the daily token cap never saw a capture that failed after spending.
    import time

    from memorymaster.dreaming.providers import consolidation_from_raw

    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    _extracted_capture(ledger)

    class DropsDecision(_Consolidator):
        calls = 0

        def consolidate(self, candidates, current_claims, *, scope):
            type(self).calls += 1
            return consolidation_from_raw(
                '{"decisions": []}', candidates, started=time.monotonic(),
                input_tokens=50_000, output_tokens=5, provider=self.provider, model=self.model,
            )

    worker = DreamWorker(ledger, service, _NoCall(), DropsDecision(),
                         config=DreamConfig(max_semantic_attempts=5, max_input_tokens_daily=40_000),
                         now=lambda: NOW)
    worker.run(apply_candidates=False)
    assert ledger.provider_input_tokens_today("zai", now=NOW) >= 50_000
    second = worker.run(apply_candidates=False)
    assert DropsDecision.calls == 1
    assert second["deferred_consolidate_budget"] == 1


def test_transient_provider_failures_record_estimated_input_tokens(tmp_path):
    from memorymaster.dreaming.providers import consolidation_prompt

    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    _capture(ledger, session_hash="fresh")
    _extracted_capture(ledger, session_hash="extracted")

    class Broken(_Extractor):
        def extract(self, messages, *, scope, capture_hash):
            raise ProviderCallError("provider request failed")

    class Seen(_Consolidator):
        prompts: list[str] = []

        def consolidate(self, candidates, current_claims, *, scope):
            type(self).prompts.append(consolidation_prompt(candidates, current_claims, scope))
            raise ProviderCallError("provider request failed")

    DreamWorker(ledger, service, Broken(), Seen(), config=DreamConfig(), now=lambda: NOW).run(apply_candidates=False)
    with ledger._connect() as conn:
        rows = conn.execute(
            "SELECT provider, outcome, input_tokens FROM dream_provider_usage ORDER BY id"
        ).fetchall()
    usage = {(row[0], row[1]): int(row[2]) for row in rows}
    assert len(rows) == 2
    assert usage[("google", "error")] > 0
    assert usage[("zai", "error")] >= len(Seen.prompts[0]) // 4 > 0


def test_post_call_validation_failure_is_recorded_as_one_call(tmp_path):
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    _extracted_capture(ledger)

    class Unreviewed(_Consolidator):
        def consolidate(self, candidates, current_claims, *, scope):
            return ConsolidationResult(tuple(
                DreamDecision(candidate.candidate_id, "add", "missing review", 0.9)
                for candidate in candidates
            ), _usage("zai", self.model))

    DreamWorker(ledger, service, _NoCall(), Unreviewed(), config=DreamConfig(),
                now=lambda: NOW).run(apply_candidates=False)
    with ledger._connect() as conn:
        rows = [tuple(row) for row in conn.execute(
            "SELECT outcome, input_tokens FROM dream_provider_usage WHERE provider='zai'"
        )]
    assert rows == [("ok", 20)]


def test_consolidation_packs_captures_under_default_call_budget(tmp_path):
    # Review F-06 / repro r4: one call per capture made the default daily call
    # cap (12) consolidate 12 of 20 single-candidate captures.
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    ids = [_extracted_capture(ledger, session_hash=f"s{i}", candidate_id=f"c{i}") for i in range(20)]

    class Counting(_Consolidator):
        sizes: list[int] = []

        def consolidate(self, candidates, current_claims, *, scope):
            type(self).sizes.append(len(candidates))
            return super().consolidate(candidates, current_claims, scope=scope)

    summary = DreamWorker(ledger, service, _NoCall(), Counting(), config=DreamConfig(),
                          now=lambda: NOW).run(apply_candidates=False)
    assert Counting.sizes == [5, 5, 5, 5]
    assert [ledger.get_capture(i)["state"] for i in ids] == ["consolidated"] * 20
    assert summary["consolidated"] == 20
    assert summary["deferred_consolidate_budget"] == 0


def test_unexpected_packed_failure_is_retried_per_capture_without_charging_peers(tmp_path):
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    first = _extracted_capture(ledger, session_hash="first", candidate_id="first")
    second = _extracted_capture(ledger, session_hash="second", candidate_id="second")

    class PackedUnexpected(_Consolidator):
        sizes: list[int] = []

        def consolidate(self, candidates, current_claims, *, scope):
            type(self).sizes.append(len(candidates))
            if len(candidates) > 1:
                raise RuntimeError("unexpected failure of the packed call")
            return super().consolidate(candidates, current_claims, scope=scope)

    summary = DreamWorker(ledger, service, _NoCall(), PackedUnexpected(), config=DreamConfig(),
                          now=lambda: NOW).run(apply_candidates=False)
    assert PackedUnexpected.sizes == [2, 1, 1]
    for capture_id in (first, second):
        capture = ledger.get_capture(capture_id)
        assert (capture["state"], capture["error_count"], capture["last_error"]) == ("consolidated", 0, None)
    assert summary["consolidated"] == 2
    assert summary["errors"] == 0
    assert summary["consolidate_breaker"] is None  # unexpected is not provider-wide


@pytest.mark.parametrize(("per_call", "count", "calls_before_breaker"), [
    pytest.param(1, 3, 1, id="single-capture-batches"),
    pytest.param(2, 4, 1, id="packed-batches"),
])
def test_provider_wide_consolidation_failure_stops_further_calls_in_the_run(
    tmp_path, per_call, count, calls_before_breaker,
):
    # Mirror of the extraction 429 break (operator 2026-09-23): after the first
    # transient/provider-wide consolidation failure the run issues no more
    # consolidation calls; untouched captures stay exactly as they were.
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    ids = [_extracted_capture(ledger, session_hash=f"s{i}", candidate_id=f"c{i}") for i in range(count)]
    before = {capture_id: ledger.get_capture(capture_id) for capture_id in ids}
    calls: list = []
    consolidator = _agy_consolidator(tmp_path, _agy_timeout, calls)

    summary = DreamWorker(ledger, service, _NoCall(), consolidator,
                          config=DreamConfig(max_consolidate_candidates=per_call),
                          now=lambda: NOW).run(apply_candidates=False)

    assert len(calls) == calls_before_breaker
    assert summary["consolidate_breaker"] == "TimeoutExpired"
    after = {capture_id: ledger.get_capture(capture_id) for capture_id in ids}
    if per_call == 1:
        # The capture whose own call failed is retryable, and not charged.
        first = after.pop(ids[0])
        before.pop(ids[0])
        assert (first["state"], first["error_count"]) == ("retryable", 0)
    assert after == before
    assert summary["deferred_consolidate_provider"] == len(after)
    assert summary["consolidated"] == 0


@pytest.mark.parametrize("outage", [
    pytest.param(lambda: ProviderCallError("provider request failed with HTTP 503", http_status=503), id="http-503"),
    pytest.param(lambda: ProviderCallError("provider request failed with HTTP 429", http_status=429), id="http-429"),
    pytest.param(lambda: _wrapped_antigravity("`agy` reporto status=QUOTA_EXHAUSTED"), id="antigravity-quota"),
])
def test_transient_packed_failure_defers_the_whole_batch_without_fan_out(tmp_path, outage):
    # Review F-06 follow-up (operator 2026-09-23): a provider-wide failure of a
    # packed call says nothing about any capture in it. Fanning out one call
    # per capture only multiplied the outage (and each agy call's ~20k floor).
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    ids = [_extracted_capture(ledger, session_hash=f"s{i}", candidate_id=f"c{i}") for i in range(2)]
    before = {capture_id: ledger.get_capture(capture_id) for capture_id in ids}

    class PackedOutage(_Consolidator):
        sizes: list[int] = []

        def consolidate(self, candidates, current_claims, *, scope):
            type(self).sizes.append(len(candidates))
            raise outage()

    summary = DreamWorker(ledger, service, _NoCall(), PackedOutage(), config=DreamConfig(),
                          now=lambda: NOW).run(apply_candidates=False)
    assert PackedOutage.sizes == [2]
    assert {capture_id: ledger.get_capture(capture_id) for capture_id in ids} == before
    assert (summary["consolidated"], summary["deferred_consolidate_provider"]) == (0, 2)
    with ledger._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM dream_provider_usage").fetchone()[0] == 1

    class Healthy(_Consolidator):
        sizes: list[int] = []

        def consolidate(self, candidates, current_claims, *, scope):
            type(self).sizes.append(len(candidates))
            return super().consolidate(candidates, current_claims, scope=scope)

    DreamWorker(ledger, service, _NoCall(), Healthy(), config=DreamConfig(),
                now=lambda: NOW).run(apply_candidates=False)
    assert Healthy.sizes == [2]
    assert [ledger.get_capture(i)["state"] for i in ids] == ["consolidated", "consolidated"]


def test_consolidation_breaker_keeps_the_error_text_for_diagnosis(tmp_path):
    # Review B1 follow-up: a packed provider-wide deferral leaves the captures
    # untouched, and the breaker label is only the root exception type, so
    # quota, timeout and other agy failures looked identical. The run summary
    # keeps the (truncated) error text.
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    for i in range(2):
        _extracted_capture(ledger, session_hash=f"s{i}", candidate_id=f"c{i}")
    calls: list = []

    summary = DreamWorker(ledger, service, _NoCall(), _agy_consolidator(tmp_path, _agy_quota, calls),
                          config=DreamConfig(), now=lambda: NOW).run(apply_candidates=False)

    assert (len(calls), summary["consolidate_breaker"]) == (1, "AntigravityError")
    assert "QUOTA_EXHAUSTED: quota resets at 00:00" in summary["consolidate_breaker_detail"]
    assert len(summary["consolidate_breaker_detail"]) <= 300


def _wrapped_antigravity(message: str) -> ProviderCallError:
    from memorymaster.core.antigravity_client import AntigravityError

    try:
        raise ProviderCallError(message) from AntigravityError(message)
    except ProviderCallError as error:
        return error


def _agy_ignores_every_candidate(prompt_sizes: list[int]):
    """A healthy `agy`: SUCCESS with one ignore decision per candidate."""
    def runner(command, payload, timeout, cwd, env):
        import json
        import subprocess

        prompt = json.loads(payload)["message"]["content"]
        prompt_sizes.append(len(prompt))
        ids = json.loads(prompt.split("\n\nINPUT:\n", 1)[1])["valid_candidate_ids"]
        decisions = [{"candidate_id": candidate_id, "action": "ignore", "rationale": "fixture",
                      "confidence": 0.5} for candidate_id in ids]
        event = {"event": "result", "result": {
            "status": "SUCCESS", "response": json.dumps({"decisions": decisions}),
            "usage": {"input_tokens": 20, "output_tokens": 5},
        }}
        return subprocess.CompletedProcess(command, 0, json.dumps(event), "")
    return runner


def _usage_outcomes(ledger: DreamLedger) -> list[str]:
    with ledger._connect() as conn:
        return [row[0] for row in conn.execute("SELECT outcome FROM dream_provider_usage ORDER BY id")]


def test_reference_context_over_the_agy_prompt_cap_is_trimmed_instead_of_stalling(tmp_path):
    # Review B1: max_context_chars (512k) of references alone exceeds the agy
    # client's fixed 400k prompt cap, which refuses before sending. Classified
    # provider-wide, that refusal opened the breaker on every run: the batch was
    # never charged, and every later batch or scope in the run was skipped.
    import random

    from memorymaster.core.antigravity_client import _MAX_PROMPT_CHARS

    service = _service(tmp_path)
    words = ("deployment staging cluster rollout procedure review operator durable decision "
             "database migration index recall ranking memory steward lifecycle capture").split()
    for index in range(110):  # ~4.4k chars each: about 450k chars of reference context
        rng = random.Random(index)
        service.ingest("Reference note: " + " ".join(rng.choice(words) for _ in range(520)) + ".",
                       [CitationInput("test")], scope="project:a", source_agent="test",
                       claim_type="decision")
    ledger = DreamLedger(tmp_path / "capture.db")
    crowded = _extracted_capture(ledger, scope="project:a", session_hash="a", candidate_id="a-fact")
    other = _extracted_capture(ledger, scope="project:b", session_hash="b", candidate_id="b-fact")
    sizes: list[int] = []
    consolidator = _agy_consolidator(tmp_path, _agy_ignores_every_candidate(sizes), [])

    summary = DreamWorker(ledger, service, _NoCall(), consolidator, config=DreamConfig(),
                          now=lambda: NOW).run(apply_candidates=False)

    assert summary["consolidate_breaker"] is None
    assert [ledger.get_capture(i)["state"] for i in (crowded, other)] == ["consolidated", "consolidated"]
    assert len(sizes) == 2
    # Trimmed to fit, keeping as much reference context as fits (one claim of slack).
    assert _MAX_PROMPT_CHARS - 10_000 < max(sizes) <= _MAX_PROMPT_CHARS


def test_batch_that_cannot_fit_the_agy_prompt_cap_counts_without_a_call(tmp_path, monkeypatch):
    # Review B1: even with no reference claims this capture's own prompt is over
    # the cap. That is deterministic (the client calls it an assembly error), so
    # it counts like an unexpected error, is never sent or charged, and opens no
    # provider-wide breaker for the other scopes.
    from memorymaster.core import antigravity_client

    monkeypatch.setattr(antigravity_client, "_MAX_PROMPT_CHARS", 14_000)
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    oversized = _extracted_capture(ledger, scope="project:a", session_hash="big", candidate_id="big-fact",
                                   extra_text=" Long transcript detail." * 800)
    other = _extracted_capture(ledger, scope="project:b", session_hash="small", candidate_id="small-fact")
    sizes: list[int] = []
    consolidator = _agy_consolidator(tmp_path, _agy_ignores_every_candidate(sizes), [])
    worker = DreamWorker(ledger, service, _NoCall(), consolidator,
                         config=DreamConfig(max_capture_errors=2), now=lambda: NOW)

    summary = worker.run(apply_candidates=False)
    assert summary["consolidate_breaker"] is None
    assert ledger.get_capture(other)["state"] == "consolidated"
    row = ledger.get_capture(oversized)
    assert (row["state"], row["error_count"]) == ("retryable", 1)
    assert "prompt" in row["last_error"]
    assert len(sizes) == 1  # only the capture that fits was sent
    assert _usage_outcomes(ledger) == ["ok"]  # nothing charged for the unsent prompt

    worker.run(apply_candidates=False)
    row = ledger.get_capture(oversized)
    assert (row["state"], row["error_count"]) == ("quarantined", 2)
    assert (len(sizes), _usage_outcomes(ledger)) == (1, ["ok"])


def test_packed_batch_over_the_agy_prompt_cap_is_split_per_capture(tmp_path, monkeypatch):
    # Review B1: a packed batch that only fits capture by capture is split, not
    # deferred behind an open breaker.
    from memorymaster.core import antigravity_client

    monkeypatch.setattr(antigravity_client, "_MAX_PROMPT_CHARS", 14_000)
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    ids = [_extracted_capture(ledger, session_hash=f"s{i}", candidate_id=f"c{i}",
                              extra_text=" Transcript detail." * 250) for i in range(2)]
    sizes: list[int] = []
    consolidator = _agy_consolidator(tmp_path, _agy_ignores_every_candidate(sizes), [])

    summary = DreamWorker(ledger, service, _NoCall(), consolidator, config=DreamConfig(),
                          now=lambda: NOW).run(apply_candidates=False)

    assert summary["consolidate_breaker"] is None
    assert len(sizes) == 2 and max(sizes) <= 14_000
    for capture_id in ids:
        row = ledger.get_capture(capture_id)
        assert (row["state"], row["error_count"]) == ("consolidated", 0)
    assert _usage_outcomes(ledger) == ["ok", "ok"]


def test_packed_semantic_failure_charges_only_the_invalid_capture(tmp_path):
    import json
    import time

    from memorymaster.dreaming.providers import consolidation_from_raw

    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    bad = _extracted_capture(ledger, session_hash="bad", candidate_id="bad")
    good = _extracted_capture(ledger, session_hash="good", candidate_id="good")

    class Mixed(_Consolidator):
        sizes: list[int] = []

        def consolidate(self, candidates, current_claims, *, scope):
            type(self).sizes.append(len(candidates))
            result = super().consolidate(candidates, current_claims, scope=scope)
            for decision in result.decisions:
                if decision.candidate_id == "bad":
                    decision.source_review["source_hash"] = "wrong-source-hash"
            return consolidation_from_raw(
                json.dumps({"decisions": [d.to_dict() for d in result.decisions]}),
                candidates, started=time.monotonic(), input_tokens=20, output_tokens=5,
                provider=self.provider, model=self.model,
            )

    DreamWorker(ledger, service, _NoCall(), Mixed(), config=DreamConfig(max_semantic_attempts=2),
                now=lambda: NOW).run(apply_candidates=False)
    assert Mixed.sizes == [2, 1, 1]
    good_row, bad_row = ledger.get_capture(good), ledger.get_capture(bad)
    assert (good_row["state"], good_row["error_count"], good_row["last_error"]) == ("consolidated", 0, None)
    assert (bad_row["state"], bad_row["error_count"]) == ("retryable", 1)
    assert bad_row["last_error"].startswith("semantic-failure-v1:consolidate:1:")


def _slow_lease_fixture(tmp_path, count: int = 6):
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    ids = [_extracted_capture(ledger, session_hash=f"s{i}", candidate_id=f"c{i}") for i in range(count)]
    return service, ledger, ids


def test_lease_is_renewed_per_batch_so_second_worker_cannot_take_over(tmp_path):
    # Review F-07 / repro r6: the lease was acquired once; four 300 s calls
    # outlived the 900 s TTL and a second worker double-processed the queue.
    service, ledger, ids = _slow_lease_fixture(tmp_path)
    clock = [NOW]
    calls = {"A": 0, "B": 0}
    second: dict = {}
    config = DreamConfig(max_consolidate_candidates=1)

    class Slow(_Consolidator):
        def __init__(self, tag):
            super().__init__()
            self.tag = tag

        def consolidate(self, candidates, current_claims, *, scope):
            calls[self.tag] += 1
            clock[0] += timedelta(seconds=300)
            if self.tag == "A" and calls["A"] == 4:
                worker_b = DreamWorker(ledger, service, _NoCall(), Slow("B"), config=config, now=lambda: clock[0])
                second["summary"] = worker_b.run(apply_candidates=False)
            return super().consolidate(candidates, current_claims, scope=scope)

    summary = DreamWorker(ledger, service, _NoCall(), Slow("A"), config=config,
                          now=lambda: clock[0]).run(apply_candidates=False)
    assert second["summary"] == {"ok": False, "reason": "worker_busy"}
    assert calls == {"A": 6, "B": 0}
    assert summary["consolidated"] == 6
    assert {ledger.get_capture(i)["run_id"] for i in ids} == {summary["run_id"]}


def test_worker_that_lost_its_lease_writes_no_capture_state(tmp_path):
    service, ledger, ids = _slow_lease_fixture(tmp_path)
    clock = [NOW]
    calls = {"A": 0, "B": 0}
    second: dict = {}

    class Stalled(_Consolidator):
        def __init__(self, tag):
            super().__init__()
            self.tag = tag

        def consolidate(self, candidates, current_claims, *, scope):
            calls[self.tag] += 1
            if self.tag == "A":
                # One call outlives the 900 s lease; B legitimately takes over.
                clock[0] += timedelta(seconds=1000)
                worker_b = DreamWorker(ledger, service, _NoCall(), Stalled("B"),
                                       config=DreamConfig(), now=lambda: clock[0])
                second["summary"] = worker_b.run(apply_candidates=False)
            return super().consolidate(candidates, current_claims, scope=scope)

    summary = DreamWorker(ledger, service, _NoCall(), Stalled("A"), config=DreamConfig(),
                          now=lambda: clock[0]).run(apply_candidates=False)
    assert second["summary"]["ok"] is True
    assert second["summary"]["consolidated"] == 6
    assert summary["ok"] is False
    assert summary["reason"] == "lease_lost"
    assert calls["A"] == 1
    assert {ledger.get_capture(i)["run_id"] for i in ids} == {second["summary"]["run_id"]}
    with ledger._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM dream_leases").fetchone()[0] == 0
        spent = conn.execute(
            "SELECT COUNT(*) FROM dream_provider_usage WHERE run_id=?", (summary["run_id"],)
        ).fetchone()[0]
    # The spend of the stalled call is still accounted for.
    assert spent == 1


def test_structurally_invalid_provider_output_is_bounded(tmp_path):
    # A provider call that succeeds but returns unusable content fails the same
    # way on every retry; it must reach quarantine instead of paying forever.
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    candidate = _Extractor().extract([], scope="project:test", capture_hash="a").candidates[0]
    ledger.set_extraction(capture_id, [candidate.to_dict()], "fixture")

    class EmptyDecisions(_Consolidator):
        def consolidate(self, candidates, current_claims, *, scope):
            from memorymaster.dreaming.providers import consolidation_from_raw
            return consolidation_from_raw(
                '{"decisions": []}', candidates, started=0.0, input_tokens=0,
                output_tokens=0, provider="zai", model="fixture",
            )

    worker = DreamWorker(ledger, service, _NoCall(), EmptyDecisions(),
                         config=DreamConfig(max_semantic_attempts=2), now=lambda: NOW)
    worker.run(apply_candidates=False)
    assert ledger.get_capture(capture_id)["state"] == "retryable"
    worker.run(apply_candidates=False)
    capture = ledger.get_capture(capture_id)
    assert capture["state"] == "quarantined"
    assert capture["last_error"].startswith("semantic-failure-v1:consolidate:2:")


def test_provider_output_error_is_semantic_and_still_a_provider_error():
    from memorymaster.dreaming.providers import ProviderCallError, ProviderOutputError, _json_object

    for raw in ("not json", "[1, 2]"):
        with pytest.raises(ProviderOutputError) as caught:
            _json_object(raw)
        assert isinstance(caught.value, ValueError)
        assert isinstance(caught.value, ProviderCallError)


def test_invalid_review_does_not_quarantine_valid_capture_in_same_batch(tmp_path):
    from dataclasses import replace
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    bad_id = _capture(ledger, session_hash="bad")
    good_id = _capture(ledger, session_hash="good")
    original = _Extractor().extract([], scope="project:test", capture_hash="a").candidates[0]
    ledger.set_extraction(bad_id, [replace(original, candidate_id="bad").to_dict()], "fixture")
    ledger.set_extraction(good_id, [replace(original, candidate_id="good").to_dict()], "fixture")

    class Mixed(_Consolidator):
        def consolidate(self, candidates, current_claims, *, scope):
            import json
            import time
            from memorymaster.dreaming.providers import consolidation_from_raw
            result = super().consolidate(candidates, current_claims, scope=scope)
            for decision in result.decisions:
                if "bad" in decision.candidate_id:
                    decision.source_review["source_hash"] = "wrong-source-hash"
            return consolidation_from_raw(
                json.dumps({"decisions": [d.to_dict() for d in result.decisions]}),
                candidates, started=time.monotonic(), input_tokens=20, output_tokens=5,
                provider=self.provider, model=self.model,
            )

    worker = DreamWorker(ledger, service, _NoCall(), Mixed(),
                         config=DreamConfig(max_semantic_attempts=2), now=lambda: NOW)
    worker.run(apply_candidates=True)
    assert ledger.get_capture(bad_id)["state"] == "retryable"
    assert ledger.get_capture(good_id)["state"] == "applied"
    worker.run(apply_candidates=True)
    assert ledger.get_capture(bad_id)["state"] == "quarantined"
    assert ledger.get_capture(good_id)["state"] == "applied"
    assert len(service.list_claims(limit=20)) == 1


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
            decision = DreamDecision(
                "replace-c",
                "propose_supersede",
                "newer evidence",
                0.95,
                old.id,
                source_review=_accepted_review(candidates[0], scope),
            )
            return ConsolidationResult((decision,), _usage("zai", self.model))

    DreamWorker(ledger, service, OneExtractor(), Proposer(), config=DreamConfig(), now=lambda: NOW).run(apply_candidates=True)

    assert service.store.get_claim(old.id).status == "confirmed"
    events = service.list_events(claim_id=old.id, event_type="policy_decision", limit=20)
    assert any(event.details == "steward_proposal:superseded_candidate" for event in events)


def test_later_batch_sees_pending_same_scope_peer_and_duplicate_is_not_written(tmp_path):
    from dataclasses import replace

    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / 'capture.db')
    first_id = _capture(ledger, session_hash='first')
    second_id = _capture(ledger, session_hash='second')
    original = _Extractor().extract([], scope='project:test', capture_hash='a').candidates[0]
    ledger.set_extraction(first_id, [original.to_dict()], 'fixture')
    duplicate = replace(original, candidate_id='paraphrase', text='This project selected the blue interface.')
    ledger.set_extraction(second_id, [duplicate.to_dict()], 'fixture')

    class Peers(_Consolidator):
        def __init__(self):
            super().__init__()
            self.references = []

        def consolidate(self, candidates, current_claims, *, scope):
            self.references.append(current_claims)
            return super().consolidate(candidates, current_claims, scope=scope)

    peers = Peers()
    worker = DreamWorker(ledger, service, _NoCall(), peers,
                         config=DreamConfig(max_consolidate_candidates=1), now=lambda: NOW)
    result = worker.run(apply_candidates=True)
    assert result['errors'] == 0
    assert len(peers.references) == 2
    assert peers.references[0] == []
    assert peers.references[1][0]['text'] == original.text
    assert peers.references[1][0]['scope'] == 'project:test'
    assert peers.references[1][0]['id'] is None
    assert result['candidate_writes'] == 1
    assert result['duplicate_ignored'] == 1
    assert len(service.list_claims(limit=20)) == 1


def test_pending_legacy_review_is_reconsolidated_without_reextracting(tmp_path):
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / 'capture.db')
    capture_id = _capture(ledger)
    old = _Extractor().extract([], scope='project:test', capture_hash='a').candidates[0]
    ledger.set_extraction(capture_id, [old.to_dict()], 'fixture')
    ledger.set_decisions(capture_id, [DreamDecision(old.candidate_id, 'add', 'old', .9,
                                                  source_review={'version': 1, 'verdict': 'accept'}).to_dict()], 'fixture')
    ledger.mark_retryable(capture_id, 'fixture', 'interrupted')
    consolidator = _Consolidator()
    result = DreamWorker(ledger, service, _NoCall(), consolidator, config=DreamConfig(),
                         now=lambda: NOW).run(apply_candidates=True)
    assert result['errors'] == 0
    assert len(consolidator.scopes) == 1
    assert result['candidate_writes'] == 1
    assert ledger.get_capture(capture_id)['decisions'][0]['source_review']['version'] == 2
