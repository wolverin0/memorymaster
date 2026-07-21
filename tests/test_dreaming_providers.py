from __future__ import annotations

import pytest

from memorymaster.dreaming.models import DreamCandidate, candidate_from_payload
from memorymaster.dreaming.providers import GLMConsolidator, GeminiExtractor, ProviderCallError


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


def test_glm_consolidator_uses_single_flight_json_reasoning_contract() -> None:
    seen: dict = {}

    def transport(url, payload, headers, timeout):
        seen.update({"url": url, "payload": payload, "timeout": timeout})
        return 200, {"choices": [{"message": {"content": '{"decisions":[{"candidate_id":"c1","action":"add","rationale":"new stable preference","confidence":0.9}]}'}}], "usage": {"prompt_tokens": 20, "completion_tokens": 8}}, {}

    candidate = DreamCandidate(
        candidate_id="c1",
        text="The user prefers blue interfaces.",
        claim_type="preference",
        subject="user",
        predicate="prefers",
        object_value="blue interfaces",
        scope_class="personal",
        evidence_message_id="m1",
        evidence_quote="prefers blue",
        confidence=0.8,
    )
    consolidator = GLMConsolidator(api_key="test-key", transport=transport, sleep=lambda _: None)

    result = consolidator.consolidate([candidate], [], scope="personal")

    assert seen["payload"]["model"] == "glm-5.2"
    assert seen["payload"]["response_format"] == {"type": "json_object"}
    assert seen["payload"]["thinking"] == {"type": "enabled"}
    assert seen["payload"]["reasoning_effort"] == "high"
    assert result.decisions[0].action == "add"


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
    def transport(url, payload, headers, timeout):
        del url, payload, headers, timeout
        decisions = {
            "decisions": [
                {"candidate_id": "c1", "action": "add", "rationale": "new", "confidence": 0.9},
                {"candidate_id": "c1", "action": "ignore", "rationale": "duplicate", "confidence": 0.2},
            ]
        }
        return 200, {"choices": [{"message": {"content": __import__("json").dumps(decisions)}}]}, {}

    candidate = DreamCandidate(
        candidate_id="c1",
        text="The user prefers blue interfaces.",
        claim_type="preference",
        subject="user",
        predicate="prefers",
        object_value="blue interfaces",
        scope_class="personal",
        evidence_message_id="m1",
        evidence_quote="prefers blue",
        confidence=0.8,
    )

    with pytest.raises(ProviderCallError, match="exactly one decision"):
        GLMConsolidator(api_key="test-key", transport=transport).consolidate(
            [candidate], [], scope="personal"
        )
