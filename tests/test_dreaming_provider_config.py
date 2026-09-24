"""Ruling R1 (4.9.0): configuration/auth provider failures stop the Dreaming run.

Missing API key, Gemini HTTP 400 for an invalid or expired key, 401, 403, 404
(model not found), a retired provider and a missing CLI say nothing about the
capture being processed. They are provider-wide, never charged to a capture
(no error_count, no semantic count, no state change) and stop the run: no
further extraction, no consolidation and no application in that run.
"""
from __future__ import annotations

import json

import pytest

from memorymaster.dreaming import providers
from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.providers import ProviderCallError
from memorymaster.dreaming.worker import DreamConfig, DreamWorker

from test_dreaming_worker import (  # noqa: E402  (shared fixtures of the worker suite)
    NOW,
    _capture,
    _Consolidator,
    _extracted_capture,
    _Extractor,
    _NoCall,
    _service,
)


def _gemini_body(message: str, reason: str | None = None, status: str = "INVALID_ARGUMENT") -> dict:
    error: dict = {"code": 400, "message": message, "status": status}
    if reason:
        error["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason,
                             "domain": "googleapis.com"}]
    return {"error": error}


def _gemini(status: int, body: dict | None = None, *, api_key: str = "fixture-key"):
    calls: list[str] = []

    def transport(url, payload, headers, timeout):
        calls.append("post")
        return status, body or {"error": {"message": "fixture"}}, {}

    extractor = providers.GeminiExtractor(api_key=api_key, model="gemini-test", transport=transport,
                                          sleep=lambda _seconds: None)
    return extractor, calls


INVALID_KEY = _gemini_body("API key not valid. Please pass a valid API key.", "API_KEY_INVALID")
EXPIRED_KEY = _gemini_body("API key expired. Please renew the API key.", "API_KEY_EXPIRED")
# Ruling R1 extension: HTTP 400 FAILED_PRECONDITION is the project's configuration
# (region or billing), never the capture's content.
UNSUPPORTED_LOCATION = _gemini_body("User location is not supported for the API use.", status="FAILED_PRECONDITION")
BILLING_REQUIRED = _gemini_body("Gemini API free tier is not available in your country. Please enable billing on "
                                "your project in Google AI Studio.", status="FAILED_PRECONDITION")
OTHER_PRECONDITION = _gemini_body("Precondition check failed.", status="FAILED_PRECONDITION")


# ------------------------------------------------------------ provider layer ---

@pytest.mark.parametrize(("status", "body", "reason"), [
    pytest.param(400, INVALID_KEY, "invalid_api_key", id="http-400-invalid-key"),
    pytest.param(400, EXPIRED_KEY, "expired_api_key", id="http-400-expired-key"),
    pytest.param(400, _gemini_body("API key not valid. Please pass a valid API key."), "invalid_api_key",
                 id="http-400-invalid-key-message-only"),
    pytest.param(400, UNSUPPORTED_LOCATION, "unsupported_location", id="http-400-unsupported-location"),
    pytest.param(400, BILLING_REQUIRED, "billing_required", id="http-400-billing-required"),
    pytest.param(400, OTHER_PRECONDITION, "failed_precondition", id="http-400-failed-precondition"),
    pytest.param(401, None, "unauthorized", id="http-401"),
    pytest.param(403, None, "forbidden", id="http-403"),
    pytest.param(404, _gemini_body("models/gemini-test is not found for API version v1beta", status="NOT_FOUND"),
                 "model_not_found", id="http-404-model-not-found"),
])
def test_gemini_configuration_failures_are_config_errors(status, body, reason):
    extractor, calls = _gemini(status, body)
    with pytest.raises(providers.ProviderConfigError) as raised:
        extractor.extract([{"id": "m1", "role": "user", "text": "fixture"}], scope="project:test",
                          capture_hash="h")
    assert raised.value.reason == reason
    assert raised.value.http_status == status
    assert calls == ["post"]  # configuration failures are never retried
    assert "fixture-key" not in str(raised.value)


def test_missing_gemini_key_is_a_config_error_before_any_request():
    extractor, calls = _gemini(200, api_key="")
    with pytest.raises(providers.ProviderConfigError) as raised:
        extractor.extract([], scope="project:test", capture_hash="h")
    assert (raised.value.reason, raised.value.http_status, calls) == ("missing_api_key", 0, [])


@pytest.mark.parametrize("body", [
    pytest.param(_gemini_body("Request payload size exceeds the limit: 20971520 bytes."), id="payload-too-large"),
    pytest.param(_gemini_body("Invalid JSON payload received."), id="invalid-json-payload"),
    pytest.param({"error": {"message": "fixture"}}, id="unspecified"),
])
def test_other_http_400_stays_a_capture_level_error(body):
    extractor, _calls = _gemini(400, body)
    with pytest.raises(ProviderCallError) as raised:
        extractor.extract([], scope="project:test", capture_hash="h")
    assert not isinstance(raised.value, providers.ProviderConfigError)
    assert raised.value.http_status == 400


def test_retired_provider_and_missing_cli_are_config_errors(tmp_path, monkeypatch):
    with pytest.raises(providers.ProviderConfigError) as retired:
        providers._opencode_environment("zai")
    assert retired.value.reason == "retired_provider"
    with pytest.raises(providers.ProviderConfigError):
        providers.OpenCodeExtractor(model="zai-coding-plan/glm-5.2")

    monkeypatch.setattr(providers.shutil, "which", lambda _name: None)
    opencode = providers.OpenCodeExtractor(model="openai/gpt-test", command=None, work_dir=tmp_path / "oc")
    opencode.command = None
    with pytest.raises(providers.ProviderConfigError) as missing:
        opencode.extract([], scope="project:test", capture_hash="h")
    assert missing.value.reason == "cli_missing"

    def not_found(*_args):
        raise FileNotFoundError("opencode")

    spawned = providers.OpenCodeExtractor(model="openai/gpt-test", command="opencode", runner=not_found,
                                          work_dir=tmp_path / "oc")
    with pytest.raises(providers.ProviderConfigError) as vanished:
        spawned.extract([], scope="project:test", capture_hash="h")
    assert vanished.value.reason == "cli_missing"


def test_missing_agy_cli_is_a_config_error_for_consolidation(tmp_path):
    from memorymaster.core.antigravity_client import AntigravityClient

    client = AntigravityClient(model="gemini-test", command="memorymaster-no-such-agy-cli",
                               work_dir=tmp_path / "agy")
    consolidator = providers.AntigravityConsolidator(model="gemini-test", client=client)
    with pytest.raises(providers.ProviderConfigError) as raised:
        consolidator.consolidate([], [], scope="project:test")
    assert raised.value.reason == "cli_missing"


# -------------------------------------------------------------- worker layer ---

class _Counting:
    """Delegates to a real provider object and counts stage invocations."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.provider = inner.provider
        self.model = inner.model
        self.calls = 0

    def extract(self, messages, *, scope, capture_hash):
        self.calls += 1
        return self.inner.extract(messages, scope=scope, capture_hash=capture_hash)


def _gemini_http(status, body=None):
    return lambda tmp_path, monkeypatch: _gemini(status, body)[0]


def _gemini_missing_key(tmp_path, monkeypatch):
    return _gemini(200, api_key="")[0]


def _retired(tmp_path, monkeypatch):
    class Retired(_Extractor):
        def extract(self, messages, *, scope, capture_hash):
            providers._opencode_environment("zai")
            raise AssertionError("unreachable")
    return Retired()


def _cli_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(providers.shutil, "which", lambda _name: None)
    extractor = providers.OpenCodeExtractor(model="openai/gpt-test", command=None, work_dir=tmp_path / "oc")
    extractor.command = None
    return extractor


EXTRACTION_CONFIG_FAILURES = [
    pytest.param(_gemini_missing_key, "missing_api_key", False, id="missing-api-key"),
    pytest.param(_gemini_http(400, INVALID_KEY), "invalid_api_key", True, id="http-400-invalid-key"),
    pytest.param(_gemini_http(400, EXPIRED_KEY), "expired_api_key", True, id="http-400-expired-key"),
    pytest.param(_gemini_http(400, UNSUPPORTED_LOCATION), "unsupported_location", True,
                 id="http-400-unsupported-location"),
    pytest.param(_gemini_http(400, BILLING_REQUIRED), "billing_required", True, id="http-400-billing-required"),
    pytest.param(_gemini_http(401), "unauthorized", True, id="http-401"),
    pytest.param(_gemini_http(403), "forbidden", True, id="http-403"),
    pytest.param(_gemini_http(404), "model_not_found", True, id="http-404-model-not-found"),
    pytest.param(_retired, "retired_provider", False, id="retired-provider"),
    pytest.param(_cli_missing, "cli_missing", False, id="opencode-cli-missing"),
]


def _run_rows(ledger: DreamLedger) -> list[dict]:
    with ledger._connect() as conn:
        return [dict(row) for row in conn.execute("SELECT status, summary_json FROM dream_runs ORDER BY started_at")]


def _usage(ledger: DreamLedger) -> list[tuple]:
    with ledger._connect() as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT provider, outcome, http_status, input_tokens FROM dream_provider_usage ORDER BY id")]


@pytest.mark.parametrize(("build", "reason", "reached_provider"), EXTRACTION_CONFIG_FAILURES)
def test_extraction_config_failure_stops_the_run_and_never_charges_captures(
    tmp_path, monkeypatch, build, reason, reached_provider,
):
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    fresh = [_capture(ledger, session_hash="first"), _capture(ledger, session_hash="second")]
    extracted = _extracted_capture(ledger, session_hash="already-extracted")
    ids = [*fresh, extracted]
    before = {capture_id: ledger.get_capture(capture_id) for capture_id in ids}
    extractor = _Counting(build(tmp_path, monkeypatch))
    worker = DreamWorker(ledger, service, extractor, _NoCall(),
                         config=DreamConfig(max_capture_errors=1, max_semantic_attempts=1), now=lambda: NOW)

    for run in range(1, 4):
        summary = worker.run(apply_candidates=True)
        assert summary["ok"] is False, run
        assert summary["reason"] == "provider_config"
        assert summary["provider_config"]["stage"] == "extract"
        assert summary["provider_config"]["reason"] == reason
        assert (summary["extracted"], summary["consolidated"], summary["applied"]) == (0, 0, 0)
        assert extractor.calls == run  # one attempt per run, then the run stops
        # Never charged: every capture is byte-for-byte what it was.
        assert {capture_id: ledger.get_capture(capture_id) for capture_id in ids} == before, run

    runs = _run_rows(ledger)
    assert [row["status"] for row in runs] == ["failed"] * 3
    assert json.loads(runs[-1]["summary_json"])["provider_config"]["reason"] == reason
    usage = _usage(ledger)
    if reached_provider:  # the rejected request is a call, but nothing was billed
        assert [(row[1], row[3]) for row in usage] == [("error", 0)] * 3
    else:  # no request was ever sent: nothing is recorded against the budget
        assert usage == []


def test_extraction_config_failure_keeps_extractions_finished_earlier_in_the_run(tmp_path):
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    first = _capture(ledger, session_hash="first")
    second = _capture(ledger, session_hash="second")

    class ExpiresAfterOne(_Extractor):
        calls = 0

        def extract(self, messages, *, scope, capture_hash):
            type(self).calls += 1
            if type(self).calls > 1:
                raise providers.ProviderConfigError("Gemini rejected the API key", http_status=401,
                                                    reason="unauthorized")
            return super().extract(messages, scope=scope, capture_hash=capture_hash)

    summary = DreamWorker(ledger, service, ExpiresAfterOne(), _NoCall(), config=DreamConfig(),
                          now=lambda: NOW).run(apply_candidates=False)
    assert (summary["reason"], summary["extracted"]) == ("provider_config", 1)
    assert ledger.get_capture(first)["state"] == "extracted"
    second_row = ledger.get_capture(second)
    assert (second_row["state"], second_row["error_count"], second_row["attempts"]) == ("captured", 0, 0)


def _agy_missing(tmp_path, monkeypatch):
    from memorymaster.core.antigravity_client import AntigravityClient

    client = AntigravityClient(model="gemini-test", command="memorymaster-no-such-agy-cli",
                               work_dir=tmp_path / "agy")
    return providers.AntigravityConsolidator(model="gemini-test", client=client)


def _raising_consolidator(error):
    def build(tmp_path, monkeypatch):
        class Failing(_Consolidator):
            def consolidate(self, candidates, current_claims, *, scope):
                raise error
        return Failing()
    return build


@pytest.mark.parametrize(("build", "reason"), [
    pytest.param(_agy_missing, "cli_missing", id="agy-cli-missing"),
    pytest.param(_raising_consolidator(ProviderCallError("HTTP 401", http_status=401)), "unauthorized",
                 id="http-401"),
    pytest.param(_raising_consolidator(ProviderCallError("HTTP 403", http_status=403)), "forbidden",
                 id="http-403"),
    pytest.param(_raising_consolidator(ProviderCallError("HTTP 404", http_status=404)), "model_not_found",
                 id="http-404"),
])
def test_consolidation_config_failure_stops_the_run_and_never_charges_captures(
    tmp_path, monkeypatch, build, reason,
):
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    ids = [_extracted_capture(ledger, session_hash="a", candidate_id="ca", scope="project:a"),
           _extracted_capture(ledger, session_hash="b", candidate_id="cb", scope="project:b")]
    awaiting_apply = _extracted_capture(ledger, session_hash="c", candidate_id="cc", scope="project:c")
    ledger.set_decisions(awaiting_apply, [], "fixture")
    ids.append(awaiting_apply)
    before = {capture_id: ledger.get_capture(capture_id) for capture_id in ids}
    consolidator = build(tmp_path, monkeypatch)
    worker = DreamWorker(ledger, service, _NoCall(), consolidator,
                         config=DreamConfig(max_capture_errors=1, max_semantic_attempts=1), now=lambda: NOW)

    for run in range(1, 4):
        summary = worker.run(apply_candidates=True)
        assert (summary["ok"], summary["reason"]) == (False, "provider_config"), run
        assert summary["provider_config"]["stage"] == "consolidate"
        assert summary["provider_config"]["reason"] == reason
        assert summary["applied"] == 0  # the run stopped before application
        assert {capture_id: ledger.get_capture(capture_id) for capture_id in ids} == before, run
    assert [row["status"] for row in _run_rows(ledger)] == ["failed"] * 3


def test_consolidation_config_failure_keeps_batches_decided_earlier_in_the_run(tmp_path):
    service = _service(tmp_path)
    ledger = DreamLedger(tmp_path / "capture.db")
    first = _extracted_capture(ledger, session_hash="a", candidate_id="ca", scope="project:a")
    second = _extracted_capture(ledger, session_hash="b", candidate_id="cb", scope="project:b")
    before = ledger.get_capture(second)

    class RevokedAfterOne(_Consolidator):
        calls = 0

        def consolidate(self, candidates, current_claims, *, scope):
            type(self).calls += 1
            if type(self).calls > 1:
                raise providers.ProviderConfigError("agy is not installed", reason="cli_missing")
            return super().consolidate(candidates, current_claims, scope=scope)

    summary = DreamWorker(ledger, service, _NoCall(), RevokedAfterOne(), config=DreamConfig(),
                          now=lambda: NOW).run(apply_candidates=True)
    assert (summary["reason"], summary["consolidated"], summary["applied"]) == ("provider_config", 1, 0)
    assert ledger.get_capture(first)["state"] == "consolidated"  # the paid answer is kept
    assert ledger.get_capture(second) == before


def test_config_errors_are_never_charged_even_through_the_generic_path():
    from memorymaster.dreaming.worker import _is_deferral

    assert _is_deferral(providers.ProviderConfigError("x", reason="missing_api_key"))
    assert _is_deferral(ProviderCallError("HTTP 404", http_status=404))
    assert not _is_deferral(ProviderCallError("HTTP 400", http_status=400))

