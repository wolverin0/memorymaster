from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from memorymaster.dreaming.models import DreamCandidate, candidate_from_payload
from memorymaster.dreaming.providers import (
    AntigravityConsolidator,
    consolidation_prompt,
    consolidation_from_raw,
    opencode_response_text,
    GeminiExtractor,
    OpenCodeExtractor,
    OpenCodeConsolidator,
    ProviderConfigError,
    ProviderCallError,
    _default_command_runner,
    create_dream_extractor,
)


@pytest.mark.parametrize(("configured", "expected"), [
    (None, 300), ("60", 60), ("720", 720), ("1800", 1800),
    ("1", 60), ("1801", 1800), ("invalid", 300),
])
def test_consolidate_timeout_env_is_bounded(monkeypatch, configured, expected):
    monkeypatch.delenv("MEMORYMASTER_AGY_TIMEOUT", raising=False)
    if configured is None:
        monkeypatch.delenv("MEMORYMASTER_DREAM_CONSOLIDATE_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("MEMORYMASTER_DREAM_CONSOLIDATE_TIMEOUT_SECONDS", configured)
    provider = AntigravityConsolidator(model="operator-chosen-alias")
    assert provider.client.timeout == expected
    assert provider.model == provider.client.model == "operator-chosen-alias"


def test_consolidate_timeout_preserves_legacy_and_explicit_budget(monkeypatch):
    monkeypatch.delenv("MEMORYMASTER_DREAM_CONSOLIDATE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("MEMORYMASTER_AGY_TIMEOUT", "420")
    assert AntigravityConsolidator().client.timeout == 420
    monkeypatch.setenv("MEMORYMASTER_DREAM_CONSOLIDATE_TIMEOUT_SECONDS", "600")
    assert AntigravityConsolidator().client.timeout == 600
    assert AntigravityConsolidator(timeout=240).client.timeout == 240


@pytest.mark.parametrize("provider_name", ["gemini", "openai"])
@pytest.mark.parametrize("valid", [True, False])
def test_extract_provider_consolidation_reuses_configured_transport(tmp_path, provider_name, valid):
    from memorymaster.dreaming.providers import consolidation_fallback

    seen = []
    deleted_sessions = []
    candidate = DreamCandidate("c1", "Prefer readable code", "preference", "user", "prefers",
                               "readable code", "personal", "m1", "Prefer readable code", .9)
    raw = json.dumps({"decisions": [{"candidate_id": "c1" if valid else "unknown", "action": "ignore",
                                   "rationale": "fixture", "confidence": .9}]})
    if provider_name == "gemini":
        def transport(url, payload, headers, timeout):
            seen.append(payload)
            return 200, {"candidates": [{"content": {"parts": [{"text": raw}]}}]}, {}

        provider = GeminiExtractor(api_key="fixture", model="operator-extract", transport=transport)
    else:
        def runner(command, prompt, timeout, cwd, env):
            if command[1:3] == ["session", "delete"]:
                deleted_sessions.append(command[-1])
                return subprocess.CompletedProcess(command, 0, "", "")
            seen.append(prompt)
            event = {"type": "text", "sessionID": "fixture-session", "part": {"text": raw}}
            return subprocess.CompletedProcess(command, 0, json.dumps(event), "")

        provider = OpenCodeExtractor(model="openai/operator-extract", command="fixture",
                                     runner=runner, work_dir=tmp_path)
    fallback = consolidation_fallback(provider)
    if not valid:
        with pytest.raises(ValueError, match="unknown candidate"):
            fallback.consolidate([candidate], [], scope="personal")
        assert deleted_sessions == (["fixture-session"] if provider_name == "openai" else [])
        return
    result = fallback.consolidate([candidate], [], scope="personal")
    assert result.decisions[0].candidate_id == "c1"
    assert result.usage.provider == provider.provider
    assert result.usage.model == provider.model
    assert len(seen) == 1
    assert "decisions" in str(seen[0])
    if provider_name == "gemini":
        assert seen[0]["generationConfig"]["maxOutputTokens"] == 2000
        assert seen[0]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}
        assert "responseSchema" not in seen[0]["generationConfig"]
    assert deleted_sessions == (["fixture-session"] if provider_name == "openai" else [])


def test_extract_provider_consolidation_preserves_bounded_opencode(tmp_path):
    from memorymaster.dreaming.providers import consolidation_fallback, ProviderOutputError

    client, calls = _bounded_client(model="openai/operator-extract")
    provider = OpenCodeExtractor(model="openai/operator-extract", client=client, work_dir=tmp_path,
                                 max_input_tokens=10_000, max_output_tokens=1500)
    fallback = consolidation_fallback(provider)
    prompt = consolidation_prompt([], [], "project:test")
    assert fallback.rendered_input_tokens(prompt) == provider.token_counter(prompt)
    result = fallback.consolidate([], [], scope="project:test")
    assert result.usage.model == "openai/operator-extract"
    assert calls == [(prompt, 1500)]
    provider.max_input_tokens = 1
    with pytest.raises(ProviderOutputError):
        fallback.consolidate([], [], scope="project:test")
    assert len(calls) == 1


def test_extract_provider_consolidation_latches_output_violation(tmp_path):
    from memorymaster.dreaming.providers import consolidation_fallback

    client, calls = _bounded_client(model="openai/operator-extract", output_tokens=1501)
    provider = OpenCodeExtractor(model="openai/operator-extract", client=client, work_dir=tmp_path,
                                 max_input_tokens=10_000, max_output_tokens=1500)
    fallback = consolidation_fallback(provider)
    for _ in range(2):
        with pytest.raises(ProviderConfigError):
            fallback.consolidate([], [], scope="project:test")
    assert len(calls) == 1


def test_extract_provider_consolidation_unavailable_returns_none(tmp_path, monkeypatch):
    from memorymaster.dreaming.providers import consolidation_fallback

    assert consolidation_fallback(SimpleNamespace(provider="fixture", model="fixture")) is None
    monkeypatch.setattr("memorymaster.dreaming.providers.shutil.which", lambda _: None)
    provider = OpenCodeExtractor(model="openai/operator-extract", work_dir=tmp_path)
    provider.command = None
    assert consolidation_fallback(provider) is None


def _bounded_client(*, model="anthropic/claude-sonnet-4-6", raw='{"decisions":[]}', input_tokens=100, output_tokens=10):
    calls = []

    def complete(prompt, *, max_output_tokens):
        calls.append((prompt, max_output_tokens))
        return SimpleNamespace(text=raw, observed_model=model, model=model, input_tokens=input_tokens, output_tokens=output_tokens)

    return SimpleNamespace(complete_bounded=complete), calls


def _gemini_bounded_client(*, output_tokens=10, model_version="gemini-3.5-flash-lite", transport=None):
    from memorymaster.dreaming.providers import GeminiBoundedClient

    calls = []

    def fake_transport(url, payload, headers, timeout):
        calls.append((url, payload, headers, timeout))
        return 200, {
            "modelVersion": model_version,
            "candidates": [{"content": {"parts": [{"text": '{"candidates":[]}'}]}}],
            "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": output_tokens},
        }, {}

    client = GeminiBoundedClient(
        model="gemini-api/gemini-3.5-flash-lite", api_key="test-key",
        transport=transport or fake_transport, sleep=lambda _: None,
    )
    return client, calls


@pytest.mark.parametrize("output_tokens", [1499, 1500, 1501])
def test_gemini_bounded_response_enforces_output_cap(output_tokens):
    from memorymaster.dreaming.providers import _bounded_response

    client, _ = _gemini_bounded_client(output_tokens=output_tokens)
    response = client.complete_bounded("fixture prompt", max_output_tokens=1500)
    if output_tokens <= 1500:
        assert _bounded_response(response, client.model, 10_000, 1500) is None
    else:
        with pytest.raises(ProviderConfigError) as caught:
            _bounded_response(response, client.model, 10_000, 1500)
        assert caught.value.reason == "token_bound_exceeded"
        assert caught.value.output_tokens == output_tokens


def test_gemini_complete_bounded_sends_cap_and_parses_usage():
    client, calls = _gemini_bounded_client()
    response = client.complete_bounded("fixture prompt", max_output_tokens=1500)
    url, payload, headers, timeout = calls[0]
    assert "/models/gemini-3.5-flash-lite:generateContent?" in url
    assert payload["generationConfig"]["maxOutputTokens"] == 1500
    assert payload["generationConfig"]["responseMimeType"] == "application/json"
    assert payload["contents"] == [{"parts": [{"text": "fixture prompt"}]}]
    assert headers == {"Content-Type": "application/json"}
    assert timeout == 90
    assert (response.text, response.input_tokens, response.output_tokens) == ('{"candidates":[]}', 12, 10)
    assert response.model == response.observed_model == client.model


def test_gemini_complete_uses_a_default_output_cap():
    client, calls = _gemini_bounded_client()
    assert client.complete("fixture prompt").text == '{"candidates":[]}'
    assert calls[0][1]["generationConfig"]["maxOutputTokens"] == 2000


@pytest.mark.parametrize("status", [403, 503])
def test_gemini_bounded_transport_failure_retains_http_status(status):
    client, _ = _gemini_bounded_client(transport=lambda *_: (status, {"error": {"message": "PRIVATE"}}, {}))
    with pytest.raises(ProviderCallError) as caught:
        client.complete_bounded("fixture prompt", max_output_tokens=1500)
    assert caught.value.http_status == status
    assert "PRIVATE" not in str(caught.value)


def test_gemini_bounded_transport_exception_is_provider_call_error():
    def transport(*_):
        raise OSError("PRIVATE")

    client, _ = _gemini_bounded_client(transport=transport)
    with pytest.raises(ProviderCallError, match="provider request failed") as caught:
        client.complete_bounded("fixture prompt", max_output_tokens=1500)
    assert caught.value.http_status == 0
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("model_version", [None, "gemini-other"])
def test_gemini_bounded_response_does_not_invent_observed_model(model_version):
    from memorymaster.dreaming.providers import _bounded_response

    client, _ = _gemini_bounded_client(model_version=model_version)
    response = client.complete_bounded("fixture prompt", max_output_tokens=1500)
    with pytest.raises(ProviderConfigError) as caught:
        _bounded_response(response, client.model, 10_000, 1500)
    assert caught.value.reason == "model_mismatch"


def test_gemini_bounded_client_runs_bounded_extraction(tmp_path):
    client, calls = _gemini_bounded_client()
    extractor = OpenCodeExtractor(model=client.model, client=client, work_dir=tmp_path,
                                  max_input_tokens=10_000, max_output_tokens=1500, framing_tokens=512)
    result = extractor.extract([], scope="project:test", capture_hash="fixture")
    assert result.candidates == ()
    assert result.usage.model == extractor.last_observed_model == client.model
    assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 10)
    assert calls[0][1]["generationConfig"]["maxOutputTokens"] == 1500


def test_gemini_bounded_response_counts_thinking_tokens_against_cap():
    from memorymaster.dreaming.providers import _bounded_response

    body = {
        "modelVersion": "gemini-3.5-flash-lite",
        "candidates": [{"content": {"parts": [{"text": '{"candidates":[]}'}]}}],
        "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 1400, "thoughtsTokenCount": 101},
    }
    client, _ = _gemini_bounded_client(transport=lambda *_: (200, body, {}))
    response = client.complete_bounded("fixture prompt", max_output_tokens=1500)
    with pytest.raises(ProviderConfigError) as caught:
        _bounded_response(response, client.model, 10_000, 1500)
    assert caught.value.reason == "token_bound_exceeded"
    assert caught.value.output_tokens == 1501


@pytest.mark.parametrize("usage", [
    None, {}, {"promptTokenCount": 12},
    {"promptTokenCount": -1, "candidatesTokenCount": 10},
    {"promptTokenCount": 12, "candidatesTokenCount": "10"},
    {"promptTokenCount": 12, "candidatesTokenCount": 10, "thoughtsTokenCount": True},
])
def test_gemini_bounded_client_rejects_missing_or_invalid_usage(usage):
    from memorymaster.dreaming.providers import ProviderOutputError

    body = {"candidates": [{"content": {"parts": [{"text": '{}'}]}}], "usageMetadata": usage}
    client, _ = _gemini_bounded_client(transport=lambda *_: (200, body, {}))
    with pytest.raises(ProviderOutputError, match="token usage"):
        client.complete_bounded("fixture prompt", max_output_tokens=1500)


def test_gemini_bounded_client_requires_key_without_dispatch():
    from memorymaster.dreaming.providers import GeminiBoundedClient

    client = GeminiBoundedClient(model="gemini-api/gemini-3.5-flash-lite", api_key="",
                                 transport=lambda *_: pytest.fail("missing-key request dispatched"))
    with pytest.raises(ProviderConfigError) as caught:
        client.complete_bounded("fixture prompt", max_output_tokens=1500)
    assert caught.value.reason == "missing_api_key"


@pytest.mark.parametrize("model", ["anthropic/claude-sonnet-4-6", "gemini-3.5-flash-lite", "gemini-api/"])
def test_gemini_bounded_client_rejects_incompatible_models_before_dispatch(model):
    from memorymaster.dreaming.providers import GeminiBoundedClient

    with pytest.raises(ProviderConfigError) as caught:
        GeminiBoundedClient(model=model, api_key="test-key", transport=lambda *_: pytest.fail("invalid route dispatched"))
    assert caught.value.reason == "invalid_model"


def test_backfill_sonnet_requires_real_bounded_transport_before_dispatch(tmp_path):
    class Unsupported:
        def complete(self, prompt):
            pytest.fail("unsupported route was dispatched")

    provider = OpenCodeConsolidator(model="anthropic/claude-sonnet-4-6", client=Unsupported(), work_dir=tmp_path)
    with pytest.raises(ProviderConfigError) as caught:
        provider.consolidate([], [], scope="project:test")
    assert caught.value.reason == "output_limit_unsupported"


def test_backfill_sonnet_uses_exact_model_and_provider_output_cap(tmp_path):
    client, calls = _bounded_client()
    provider = OpenCodeConsolidator(model="anthropic/claude-sonnet-4-6", client=client, work_dir=tmp_path)
    result = provider.consolidate([], [], scope="project:test")
    assert calls[0][1] == 4000
    assert result.usage.model == provider.last_observed_model == "anthropic/claude-sonnet-4-6"


@pytest.mark.parametrize("changes", [{"model": "anthropic/claude-haiku-4-5"}, {"model": None}, {"input_tokens": 30_001}, {"output_tokens": 4001}])
def test_backfill_sonnet_latches_model_or_usage_violation_without_fallback(tmp_path, changes):
    client, calls = _bounded_client(**changes)
    provider = OpenCodeConsolidator(model="anthropic/claude-sonnet-4-6", client=client, work_dir=tmp_path)
    for _ in range(2):
        with pytest.raises(ProviderConfigError):
            provider.consolidate([], [], scope="project:test")
    assert len(calls) == 1


def test_backfill_rejects_framing_overrun_with_billed_usage_retained(tmp_path):
    client, _ = _bounded_client(input_tokens=5000, output_tokens=20)
    provider = OpenCodeConsolidator(model="anthropic/claude-sonnet-4-6", client=client, work_dir=tmp_path,
                                   token_counter=lambda prompt: 100, framing_tokens=20)
    with pytest.raises(ProviderConfigError) as caught:
        provider.consolidate([], [], scope="project:test")
    assert caught.value.reason == "token_bound_exceeded"
    assert (caught.value.input_tokens, caught.value.output_tokens, caught.value.http_status) == (5000, 20, 200)


def test_backfill_sonnet_rejects_malformed_output(tmp_path):
    client, _ = _bounded_client(raw="not-json")
    provider = OpenCodeConsolidator(model="anthropic/claude-sonnet-4-6", client=client, work_dir=tmp_path)
    with pytest.raises(ProviderCallError, match="malformed JSON"):
        provider.consolidate([], [], scope="project:test")


@pytest.mark.parametrize("stage", ["extract", "consolidate"])
def test_backfill_slot_latch_stops_an_already_queued_call(tmp_path, stage):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(2)
    local = threading.local()

    def counter(prompt):
        if not getattr(local, "counted", False):
            local.counted = True
            barrier.wait(timeout=5)
        return 100

    client, calls = _bounded_client(input_tokens=101)
    kwargs = {"model": "anthropic/claude-sonnet-4-6", "client": client,
              "work_dir": tmp_path, "token_counter": counter}
    provider = (OpenCodeExtractor(**kwargs, max_input_tokens=10_000, max_output_tokens=1500)
                if stage == "extract" else OpenCodeConsolidator(**kwargs))

    def call():
        with pytest.raises(ProviderConfigError):
            if stage == "extract":
                provider.extract([], scope="project:test", capture_hash="fixture")
            else:
                provider.consolidate([], [], scope="project:test")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: call(), range(2)))
    assert len(calls) == 1


def test_backfill_extraction_preflights_the_rendered_prompt_including_framing(tmp_path):
    client, calls = _bounded_client(raw='{"candidates":[]}')
    provider = OpenCodeExtractor(model="anthropic/claude-sonnet-4-6", work_dir=tmp_path,
                                 max_input_tokens=100, max_output_tokens=1500, framing_tokens=20, client=client)
    messages = [{"id": "m1", "role": "user", "text": "A durable rule."}]
    assert provider.rendered_input_tokens(messages, scope="project:test") > 100
    with pytest.raises(ProviderCallError, match="input cap"):
        provider.extract(messages, scope="project:test", capture_hash="source")
    assert calls == []


def test_backfill_extraction_passes_output_cap_to_capable_client(tmp_path):
    client, calls = _bounded_client(raw='{"candidates":[]}')
    provider = OpenCodeExtractor(model="anthropic/claude-sonnet-4-6", work_dir=tmp_path,
                                 max_input_tokens=10_000, max_output_tokens=1500, client=client)
    result = provider.extract([], scope="project:test", capture_hash="source")
    assert calls[0][1] == 1500
    assert result.usage.input_tokens == 100


def test_extraction_cleans_session_after_malformed_event_in_slot(tmp_path):
    calls = []

    def runner(command, prompt, timeout, cwd, env):
        calls.append((command, cwd))
        output = json.dumps({"sessionID": "owned-session", "type": "step_start"}) + "\nmalformed"
        return subprocess.CompletedProcess(command, 0, output, "")

    provider = OpenCodeExtractor(model="openai/test", command="opencode", work_dir=tmp_path / "slot-1", runner=runner)
    with pytest.raises(ProviderCallError, match="malformed"):
        provider.extract([], scope="project:test", capture_hash="source")
    assert calls[-1] == (["opencode", "session", "delete", "owned-session"], tmp_path / "slot-1")


def test_gemini_extractor_requests_json_and_retries_429_without_fallback() -> None:
    calls: list[dict] = []

    def transport(url, payload, headers, timeout):
        calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
        if len(calls) == 1:
            return 429, {"error": {"message": "slow down"}}, {"Retry-After": "0"}
        return 200, {"candidates": [{"content": {"parts": [{"text": '{"candidates":[{"text":"The user prefers blue interfaces.","claim_type":"preference","subject":"user","predicate":"prefers","object_value":"blue interfaces","scope_class":"personal","evidence_message_id":"m1","evidence_quote":"prefers blue"}]}' }]}}], "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 5}}, {}

    extractor = GeminiExtractor(api_key="test-key", transport=transport, sleep=lambda _: None)
    result = extractor.extract(
        [{"id": "m1", "role": "user", "text": "The user prefers blue interfaces."}],
        scope="project:test",
        capture_hash="capture",
    )

    assert len(calls) == 2
    assert calls[0]["payload"]["generationConfig"]["responseMimeType"] == "application/json"
    assert result.candidates[0].scope_class == "personal"
    assert result.usage.http_status == 200


def test_gemini_extractor_requires_complete_candidate_objects_in_response_schema() -> None:
    payload = GeminiExtractor._payload(
        "extract stable knowledge",
        [{"id": "m1", "role": "user", "text": "The user prefers blue interfaces."}],
        "project:test",
    )

    candidate_schema = payload["generationConfig"]["responseSchema"]["properties"]["candidates"]["items"]
    assert set(candidate_schema["required"]) == {
        "text",
        "claim_type",
        "subject",
        "predicate",
        "scope_class",
        "evidence_message_id",
        "evidence_quote",
    }
    assert candidate_schema["properties"]["scope_class"]["enum"] == ["project", "personal"]


def test_gemini_extractor_survives_short_retryable_provider_burst() -> None:
    statuses = iter((429, 503, 200))
    sleeps: list[float] = []

    def transport(url, payload, headers, timeout):
        del url, payload, headers, timeout
        status = next(statuses)
        if status == 200:
            return 200, {
                "candidates": [{"content": {"parts": [{"text": '{"candidates":[]}'}]}}],
            }, {}
        return status, {"error": {"message": "retry later"}}, {}

    result = GeminiExtractor(
        api_key="test-key",
        transport=transport,
        sleep=sleeps.append,
    ).extract(
        [{"id": "m1", "role": "user", "text": "Routine transient conversation."}],
        scope="project:test",
        capture_hash="capture",
    )

    assert result.candidates == ()
    assert sleeps == [1.0, 2.0]


def test_gemini_extractor_salvages_valid_rows_when_one_quote_is_invalid() -> None:
    response = {
        "candidates": [{"content": {"parts": [{"text": json.dumps({"candidates": [
            {
                "text": "The user prefers blue interfaces.",
                "claim_type": "preference",
                "subject": "user",
                "predicate": "prefers",
                "object_value": "blue interfaces",
                "scope_class": "personal",
                "evidence_message_id": "m1",
                "evidence_quote": "prefers blue",
            },
            {
                "text": "The user prefers green interfaces.",
                "claim_type": "preference",
                "subject": "user",
                "predicate": "prefers",
                "object_value": "green interfaces",
                "scope_class": "personal",
                "evidence_message_id": "m1",
                "evidence_quote": "not present anywhere",
            },
        ]})}]}}],
    }

    result = GeminiExtractor(
        api_key="test-key",
        transport=lambda *_: (200, response, {}),
    ).extract(
        [{"id": "m1", "role": "user", "text": "The user prefers blue interfaces."}],
        scope="project:test",
        capture_hash="capture",
    )

    assert [candidate.object_value for candidate in result.candidates] == ["blue interfaces"]
    assert result.usage.structured_valid is False


def test_gemini_extractor_repairs_unique_whitespace_normalized_quote() -> None:
    response = {
        "candidates": [{"content": {"parts": [{"text": json.dumps({"candidates": [{
            "text": "The user prefers blue interfaces.",
            "claim_type": "preference",
            "subject": "user",
            "predicate": "prefers",
            "object_value": "blue interfaces",
            "scope_class": "personal",
            "evidence_message_id": "m1",
            "evidence_quote": "USER   PREFERS BLUE",
        }]})}]}}],
    }

    result = GeminiExtractor(
        api_key="test-key",
        transport=lambda *_: (200, response, {}),
    ).extract(
        [{"id": "m1", "role": "user", "text": "The user prefers blue interfaces."}],
        scope="project:test",
        capture_hash="capture",
    )

    assert result.candidates[0].evidence_quote == "user prefers blue"
    assert result.usage.structured_valid is True


def test_opencode_extractor_uses_oauth_without_api_key(
    monkeypatch, tmp_path,
) -> None:
    seen: dict = {}
    commands: list[list[str]] = []

    def runner(command, prompt, timeout, cwd, env):
        commands.append(command)
        if command[1:3] == ["session", "delete"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        seen.update({
            "prompt": prompt,
            "timeout": timeout,
            "cwd": cwd,
            "env": env,
        })
        payload = {
            "candidates": [{
                "text": "The project uses SQLite.",
                "claim_type": "fact",
                "subject": "project",
                "predicate": "uses",
                "object_value": "SQLite",
                "scope_class": "project",
                "evidence_span_id": "e0",
                "confidence": 0.9,
            }],
        }
        events = [
            {
                "type": "text",
                "sessionID": "extract-session",
                "part": {"text": json.dumps(payload)},
            },
            {
                "type": "step_finish",
                "part": {"tokens": {"input": 10, "output": 5}},
            },
        ]
        return subprocess.CompletedProcess(
            command, 0, "\n".join(map(json.dumps, events)), "",
        )

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")
    monkeypatch.setenv("MEMORYMASTER_OPENCODE_AUTH_MODE", "oauth")
    monkeypatch.setenv("OPENCODE_DISABLE_DEFAULT_PLUGINS", "1")
    extractor = OpenCodeExtractor(
        model="openai/gpt-5.4-mini",
        variant="medium",
        command="opencode",
        runner=runner,
        work_dir=tmp_path,
    )

    result = extractor.extract(
        [{"id": "m1", "role": "user", "text": "The project uses SQLite."}],
        scope="project:test",
        capture_hash="capture",
    )

    assert result.candidates[0].object_value == "SQLite"
    assert result.candidates[0].evidence_message_id == "m1"
    assert result.candidates[0].evidence_quote == "The project uses SQLite."
    assert result.usage.provider == "openai"
    assert result.usage.input_tokens == 10
    assert seen["timeout"] == 180
    assert seen["cwd"] == tmp_path
    assert "OPENAI_API_KEY" not in seen["env"]
    assert "OPENCODE_DISABLE_DEFAULT_PLUGINS" not in seen["env"]
    assert "evidence_span_id" in seen["prompt"]
    assert commands[0] == [
        "opencode",
        "run",
        "--pure",
        "--dir",
        str(tmp_path),
        "--model",
        "openai/gpt-5.4-mini",
        "--variant",
        "medium",
        "--format",
        "json",
    ]
    assert commands[1] == ["opencode", "session", "delete", "extract-session"]


def test_opencode_extraction_prompt_matches_fail_closed_contract() -> None:
    prompt = OpenCodeExtractor._prompt(
        [{"id": "m1", "role": "user", "text": "The project uses SQLite."}],
        "project:test",
    )
    payload = json.loads(prompt.split("\n\nINPUT:\n", 1)[1])

    assert "claim_type must be fact, decision, preference, profile, or constraint" in prompt
    assert "Use personal only for a stable user preference, profile, or constraint" in prompt
    assert "must use project" in prompt
    assert "evidence_span_id must exactly match one supplied evidence span ID" in prompt
    assert "Never emit secrets, API keys, tokens, credentials" in prompt
    assert "Omit any candidate that cannot satisfy every rule" in prompt
    assert payload["messages"][0]["evidence_spans"] == [{
        "evidence_span_id": "e0",
        "text": "The project uses SQLite.",
    }]


def test_opencode_extractor_rejects_unknown_evidence_span(tmp_path) -> None:
    extractor = OpenCodeExtractor(command="opencode", work_dir=tmp_path)
    result = extractor._validated_result(
        json.dumps({"candidates": [{
            "text": "The project uses SQLite.",
            "claim_type": "fact",
            "subject": "project",
            "predicate": "uses",
            "object_value": "SQLite",
            "scope_class": "project",
            "evidence_span_id": "unknown",
            "confidence": 0.9,
        }]}),
        [{"id": "m1", "role": "user", "text": "The project uses SQLite."}],
        "capture",
        0.0,
        0,
        0,
    )

    assert result.candidates == ()
    assert result.usage.structured_valid is False


def test_opencode_extractor_routes_project_markers_out_of_personal_scope(
    tmp_path,
) -> None:
    extractor = OpenCodeExtractor(command="opencode", work_dir=tmp_path)
    result = extractor._validated_result(
        json.dumps({"candidates": [{
            "text": "The project implements storage in example.py.",
            "claim_type": "fact",
            "subject": "project",
            "predicate": "implements storage in",
            "object_value": "example.py",
            "scope_class": "personal",
            "evidence_span_id": "e0",
            "confidence": 0.9,
        }]}),
        [{
            "id": "m1",
            "role": "user",
            "text": "The project implements storage in example.py.",
        }],
        "capture",
        0.0,
        0,
        0,
    )

    assert result.candidates[0].scope_class == "project"
    assert result.usage.structured_valid is True


def test_opencode_extractor_does_not_coerce_ambiguous_personal_fact(
    tmp_path,
) -> None:
    extractor = OpenCodeExtractor(command="opencode", work_dir=tmp_path)
    result = extractor._validated_result(
        json.dumps({"candidates": [{
            "text": "The user lives in Argentina.",
            "claim_type": "fact",
            "subject": "user",
            "predicate": "lives in",
            "object_value": "Argentina",
            "scope_class": "personal",
            "evidence_span_id": "e0",
            "confidence": 0.9,
        }]}),
        [{
            "id": "m1",
            "role": "user",
            "text": "The user lives in Argentina.",
        }],
        "capture",
        0.0,
        0,
        0,
    )

    assert result.candidates == ()
    assert result.usage.structured_valid is False


def test_dream_extractor_factory_selects_configured_provider(monkeypatch) -> None:
    monkeypatch.setenv("MEMORYMASTER_DREAM_EXTRACT_PROVIDER", "opencode")
    assert isinstance(create_dream_extractor(), OpenCodeExtractor)

    monkeypatch.setenv("MEMORYMASTER_DREAM_EXTRACT_PROVIDER", "gemini")
    assert isinstance(create_dream_extractor(), GeminiExtractor)

    monkeypatch.setenv("MEMORYMASTER_DREAM_EXTRACT_PROVIDER", "unknown")
    with pytest.raises(ProviderCallError, match="must be gemini or opencode"):
        create_dream_extractor()


def test_glm_prompt_rejects_transient_execution_metadata() -> None:
    prompt = consolidation_prompt([], [], "global")

    assert "transient execution status" in prompt
    assert "account usernames" in prompt
    assert "global tool instructions" in prompt


def test_glm_prompt_makes_current_claims_reference_only() -> None:
    candidate = DreamCandidate(
        candidate_id="c1",
        text="The project uses SQLite.",
        claim_type="fact",
        subject="project",
        predicate="uses",
        object_value="SQLite",
        scope_class="project",
        evidence_message_id="m1",
        evidence_quote="uses SQLite",
        confidence=0.9,
    )

    prompt = consolidation_prompt(
        [candidate],
        [{"id": 45810, "text": "Reference claim"}],
        "project:test",
    )
    payload = json.loads(prompt.split("\n\nINPUT:\n", 1)[1])

    assert payload["valid_candidate_ids"] == ["c1"]
    assert payload["current_claims_reference_only"][0]["id"] == 45810
    assert "exactly 1 items" in prompt
    assert "never a source of candidate_id values" in prompt




def test_candidate_rejects_secret_hidden_outside_summary_text() -> None:
    payload = {
        "text": "The provider credential must be rotated before deployment.",
        "claim_type": "constraint",
        "subject": "provider",
        "predicate": "credential",
        "object_value": "sk-LiveSecret1234567890abcd",
        "scope_class": "project",
        "evidence_message_id": "m1",
        "evidence_quote": "credential must be rotated",
        "confidence": 0.8,
    }

    with pytest.raises(ValueError, match="sensitive material"):
        candidate_from_payload(
            payload,
            "capture",
            0,
            [{"id": "m1", "text": "The provider credential must be rotated before deployment."}],
        )


def test_candidate_rejects_non_finite_confidence() -> None:
    payload = {
        "text": "The user prefers concise status reports.",
        "claim_type": "preference",
        "subject": "user",
        "predicate": "prefers",
        "object_value": "concise status reports",
        "scope_class": "personal",
        "evidence_message_id": "m1",
        "evidence_quote": "prefers concise",
        "confidence": "nan",
    }

    with pytest.raises(ValueError, match="confidence must be finite"):
        candidate_from_payload(
            payload,
            "capture",
            0,
            [{"id": "m1", "text": "The user prefers concise status reports."}],
        )


def test_glm_rejects_duplicate_decisions_even_when_candidate_set_matches() -> None:
    # Preserve the shared validator regression after retiring the old transport.
    from types import SimpleNamespace
    decisions = {"decisions": [
        {"candidate_id": "c1", "action": "add", "rationale": "new", "confidence": 0.9},
        {"candidate_id": "c1", "action": "ignore", "rationale": "duplicate", "confidence": 0.2},
    ]}
    with pytest.raises(ProviderCallError, match="exactly one decision"):
        consolidation_from_raw(
            json.dumps(decisions), [SimpleNamespace(candidate_id="c1")],
            started=0, input_tokens=0, output_tokens=0,
            provider="antigravity", model="gemini-3.7-flash-low",
        )


def test_default_command_runner_terminates_windows_process_tree_on_timeout(
    monkeypatch, tmp_path,
) -> None:
    calls: list[tuple[str, object]] = []

    class FakeProcess:
        pid = 321
        returncode = 1

        def communicate(self, value=None, timeout=None):
            calls.append(("communicate", (value, timeout)))
            if len([call for call in calls if call[0] == "communicate"]) == 1:
                raise subprocess.TimeoutExpired(["opencode"], timeout)
            return "", ""

        def kill(self):
            calls.append(("kill", None))

    def fake_run(command, **kwargs):
        calls.append(("run", (command, kwargs)))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("memorymaster.dreaming.providers.os.name", "nt")
    monkeypatch.setattr(
        "memorymaster.dreaming.providers.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr("memorymaster.dreaming.providers.subprocess.run", fake_run)

    with pytest.raises(subprocess.TimeoutExpired):
        _default_command_runner(
            ["opencode"], "prompt", 180, tmp_path, {"NO_COLOR": "1"},
        )

    taskkill = next(value for name, value in calls if name == "run")
    assert taskkill[0] == ["taskkill", "/PID", "321", "/T", "/F"]
    assert ("kill", None) not in calls


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        ("not-json", "malformed event JSON"),
        (json.dumps({"type": "step_start", "part": {}}), "no text event"),
    ],
)
def test_glm_consolidator_rejects_invalid_opencode_event_stream(
    stdout: str, message: str,
) -> None:
    with pytest.raises(ProviderCallError, match=message):
        opencode_response_text(stdout)


def test_gemini_extractor_spaces_consecutive_calls_under_the_per_minute_limit() -> None:
    body = {"candidates": [{"content": {"parts": [{"text": '{"candidates":[]}'}]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}}
    now = [100.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    extractor = GeminiExtractor(api_key="test-key", transport=lambda *_a: (200, body, {}),
                                sleep=sleep, min_interval_s=4.5, clock=lambda: now[0])
    messages = [{"id": "m1", "role": "user", "text": "hello"}]
    extractor.extract(messages, scope="project:test", capture_hash="a")
    now[0] += 1.0  # the first call took one second
    extractor.extract(messages, scope="project:test", capture_hash="b")
    now[0] += 10.0  # already past the interval: no wait
    extractor.extract(messages, scope="project:test", capture_hash="c")

    assert sleeps == [3.5]


def test_gemini_extractor_keeps_a_rolling_minute_under_the_token_budget() -> None:
    # 2026-10-07: a 429 after only 10 calls that carried 256k input tokens in 60 s.
    body = {"candidates": [{"content": {"parts": [{"text": '{"candidates":[]}'}]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}}
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(round(seconds, 2))
        now[0] += seconds

    extractor = GeminiExtractor(api_key="test-key", transport=lambda *_a: (200, body, {}), sleep=sleep,
                                min_interval_s=0, clock=lambda: now[0], tokens_per_minute=10_000)
    big = [{"id": "m1", "role": "user", "text": "x" * 24_000}]  # ~6k estimated tokens
    extractor.extract(big, scope="project:test", capture_hash="a")
    now[0] += 10.0
    extractor.extract(big, scope="project:test", capture_hash="b")  # 12k > 10k: waits for the first to age out

    assert sleeps == [50.05]
    assert len(extractor._sent) == 1


def test_gemini_model_preflight_uses_the_injected_transport_and_maps_404(monkeypatch):
    # T-1660: a wrong model name is one countTokens call (no token charge) through the
    # injected transport, never a raw network request; 404 = unavailable, 5xx raises.
    seen = []

    def transport(url, payload, headers, timeout):
        seen.append(url)
        return (404 if "bogus" in url else 200), {}, {}

    extractor = GeminiExtractor(api_key="test-key", transport=transport, sleep=lambda _: None)
    assert extractor.model_available("bogus-model") is False
    assert extractor.model_available("gemini-ok") is True
    assert all(":countTokens" in url for url in seen) and len(seen) == 2
    # A 5xx is inconclusive (the run's breaker owns transient trouble), so the model is not declared missing.
    flaky = GeminiExtractor(api_key="test-key", transport=lambda *_: (503, {}, {}), sleep=lambda _: None)
    assert flaky.model_available("gemini-ok") is True
