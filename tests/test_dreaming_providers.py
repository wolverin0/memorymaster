from __future__ import annotations

import json
import subprocess

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


def test_glm_prompt_rejects_transient_execution_metadata() -> None:
    prompt = GLMConsolidator._prompt([], [], "global")

    assert "transient execution status" in prompt
    assert "account usernames" in prompt
    assert "global tool instructions" in prompt


def test_glm_consolidator_uses_authenticated_opencode_account_without_api_key(tmp_path) -> None:
    seen: dict = {}
    commands: list[list[str]] = []

    def runner(command, prompt, timeout, cwd, env):
        commands.append(command)
        if command[1:3] == ["session", "delete"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        seen.update({
            "command": command,
            "prompt": prompt,
            "timeout": timeout,
            "cwd": cwd,
            "env": env,
        })
        decision = {
            "decisions": [
                {
                    "candidate_id": "c1",
                    "action": "add",
                    "rationale": "new stable preference",
                    "confidence": 0.9,
                }
            ]
        }
        events = [
            {
                "type": "text",
                "sessionID": "session-owned-by-dreaming",
                "part": {"text": f"```json\n{json.dumps(decision)}\n```"},
            },
            {
                "type": "step_finish",
                "part": {
                    "tokens": {
                        "input": 20,
                        "output": 8,
                    }
                },
            },
        ]
        return subprocess.CompletedProcess(command, 0, "\n".join(map(json.dumps, events)), "")

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
    consolidator = GLMConsolidator(
        command="opencode",
        runner=runner,
        work_dir=tmp_path,
    )

    result = consolidator.consolidate([candidate], [], scope="personal")

    assert seen["command"] == [
        "opencode",
        "run",
        "--pure",
        "--dir",
        str(tmp_path),
        "--model",
        "zai-coding-plan/glm-5.2",
        "--format",
        "json",
    ]
    assert "GLM_API_KEY" not in seen["env"]
    assert seen["env"]["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"
    assert seen["env"]["OPENCODE_DISABLE_DEFAULT_PLUGINS"] == "1"
    inline_config = json.loads(seen["env"]["OPENCODE_CONFIG_CONTENT"])
    assert inline_config == {
        "instructions": [],
        "permission": "deny",
        "mcp": {
            "gitnexus": {"enabled": False},
            "playwright": {"enabled": False},
        },
    }
    assert '"candidate_id": "c1"' in seen["prompt"]
    assert result.decisions[0].action == "add"
    assert result.usage.provider == "zai-coding-plan"
    assert result.usage.input_tokens == 20
    assert result.usage.output_tokens == 8
    assert commands[1] == [
        "opencode", "session", "delete", "session-owned-by-dreaming",
    ]


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
    def runner(command, prompt, timeout, cwd, env):
        del prompt, timeout, cwd, env
        decisions = {
            "decisions": [
                {"candidate_id": "c1", "action": "add", "rationale": "new", "confidence": 0.9},
                {"candidate_id": "c1", "action": "ignore", "rationale": "duplicate", "confidence": 0.2},
            ]
        }
        event = {"type": "text", "part": {"text": json.dumps(decisions)}}
        return subprocess.CompletedProcess(command, 0, json.dumps(event), "")

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
        GLMConsolidator(command="opencode", runner=runner).consolidate(
            [candidate], [], scope="personal"
        )


def test_glm_consolidator_fails_closed_when_opencode_account_call_fails() -> None:
    def runner(command, prompt, timeout, cwd, env):
        del prompt, timeout, cwd, env
        return subprocess.CompletedProcess(command, 1, "", "credential details must not escape")

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

    with pytest.raises(ProviderCallError, match="OpenCode GLM invocation failed with exit 1") as exc:
        GLMConsolidator(command="opencode", runner=runner).consolidate(
            [candidate], [], scope="personal"
        )
    assert "credential details" not in str(exc.value)


def test_glm_consolidator_requires_opencode_cli(monkeypatch) -> None:
    monkeypatch.delenv("MEMORYMASTER_OPENCODE_COMMAND", raising=False)
    monkeypatch.setattr(
        "memorymaster.dreaming.providers.shutil.which", lambda _name: None,
    )

    with pytest.raises(ProviderCallError, match="OpenCode CLI is not installed"):
        GLMConsolidator().consolidate([], [], scope="personal")


def test_glm_consolidator_reports_bounded_timeout() -> None:
    def runner(command, prompt, timeout, cwd, env):
        del prompt, cwd, env
        raise subprocess.TimeoutExpired(command, timeout)

    with pytest.raises(ProviderCallError, match="timed out"):
        GLMConsolidator(command="opencode", runner=runner).consolidate(
            [], [], scope="personal"
        )


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
    def runner(command, prompt, timeout, cwd, env):
        del prompt, timeout, cwd, env
        return subprocess.CompletedProcess(command, 0, stdout, "")

    with pytest.raises(ProviderCallError, match=message):
        GLMConsolidator(command="opencode", runner=runner).consolidate(
            [], [], scope="personal"
        )
