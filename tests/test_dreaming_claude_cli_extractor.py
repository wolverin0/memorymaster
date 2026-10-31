"""ClaudeCliExtractor: extract through the local `claude -p` CLI (T-1828)."""
from __future__ import annotations

import json
import subprocess

import pytest

from memorymaster.dreaming.providers import (
    EXTRACTION_INSTRUCTIONS,
    ClaudeCliExtractor,
    GeminiExtractor,
    OpenCodeExtractor,
    ProviderCallError,
    ProviderConfigError,
    ProviderOutputError,
    create_dream_extractor,
)

MESSAGES = [
    {"id": "m1", "role": "user", "text": "We always run the migration before restarting the worker.", "timestamp": "t1"},
]


def _row(**overrides):
    row = {
        "text": "Run the migration before restarting the worker.",
        "claim_type": "constraint",
        "subject": "worker restart",
        "predicate": "requires",
        "object_value": "migration first",
        "scope_class": "project",
        "evidence_span_id": "e0",
        "confidence": 0.9,
    }
    row.update(overrides)
    return row


def _envelope(rows, *, is_error=False, usage=None):
    return json.dumps({
        "type": "result", "is_error": is_error, "result": json.dumps({"candidates": rows}),
        "usage": usage or {"input_tokens": 7, "cache_read_input_tokens": 100, "output_tokens": 30},
    })


def _extractor(tmp_path, stdout="", returncode=0, stderr="", seen=None, exc=None):
    def runner(command, prompt, timeout, cwd, env):
        if seen is not None:
            seen.append((command, prompt, timeout, cwd, env))
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    return ClaudeCliExtractor(command="claude-fixture", runner=runner, work_dir=tmp_path)


def test_valid_json_yields_candidates_and_usage(tmp_path):
    seen = []
    extractor = _extractor(tmp_path, _envelope([_row()]), seen=seen)
    result = extractor.extract(MESSAGES, scope="project:x", capture_hash="h1")

    assert [c.subject for c in result.candidates] == ["worker restart"]
    assert result.candidates[0].evidence_message_id == "m1"
    usage = result.usage
    assert (usage.provider, usage.model, usage.http_status) == ("claude", "haiku", 200)
    assert (usage.input_tokens, usage.output_tokens, usage.structured_valid) == (107, 30, True)
    command, prompt, timeout, cwd, env = seen[0]
    assert command == [
        "claude-fixture", "-p", "--model", "haiku", "--output-format", "json", "--no-session-persistence",
    ]
    assert prompt.startswith(EXTRACTION_INSTRUCTIONS)
    assert '"evidence_span_id": "e0"' in prompt
    assert 10 <= timeout <= 600
    assert env["MEMORYMASTER_INTERNAL_RUN"] == "1"
    assert env["MEMORYMASTER_DREAM_ENABLED"] == "0"  # the child's own capture hooks stay off
    assert "--no-session-persistence" in command


def test_bad_evidence_row_is_counted_invalid_but_valid_rows_survive(tmp_path):
    rows = [_row(evidence_span_id="e99"), _row(predicate="needs"), "not-an-object"]
    result = _extractor(tmp_path, _envelope(rows)).extract(MESSAGES, scope="project:x", capture_hash="h1")

    assert [c.predicate for c in result.candidates] == ["needs"]
    assert result.usage.structured_valid is False


def test_plain_text_json_without_envelope_uses_estimated_tokens(tmp_path):
    raw = "```json\n" + json.dumps({"candidates": [_row()]}) + "\n```"
    result = _extractor(tmp_path, raw).extract(MESSAGES, scope="project:x", capture_hash="h1")

    assert len(result.candidates) == 1
    assert result.usage.input_tokens > 0 and result.usage.output_tokens > 0


@pytest.mark.parametrize("stdout", [_envelope([]), "boom"])
def test_non_zero_exit_raises_provider_call_error(tmp_path, stdout):
    extractor = _extractor(tmp_path, stdout, returncode=1, stderr="overloaded")
    with pytest.raises(ProviderCallError, match="exit 1") as caught:
        extractor.extract(MESSAGES, scope="project:x", capture_hash="h1")
    assert not isinstance(caught.value, ProviderConfigError)


def test_error_envelope_with_zero_exit_is_a_call_error(tmp_path):
    extractor = _extractor(tmp_path, _envelope([], is_error=True))
    with pytest.raises(ProviderCallError):
        extractor.extract(MESSAGES, scope="project:x", capture_hash="h1")


def test_login_failure_is_a_config_error(tmp_path):
    envelope = json.dumps({"type": "result", "is_error": True, "result": "Please run /login"})
    with pytest.raises(ProviderConfigError) as caught:
        _extractor(tmp_path, envelope, returncode=1).extract(MESSAGES, scope="project:x", capture_hash="h1")
    assert caught.value.reason == "unauthorized"


def test_timeout_and_missing_binary(tmp_path, monkeypatch):
    timeout = subprocess.TimeoutExpired("claude", 5)
    with pytest.raises(ProviderCallError, match="timed out"):
        _extractor(tmp_path, exc=timeout).extract(MESSAGES, scope="project:x", capture_hash="h1")
    with pytest.raises(ProviderConfigError) as caught:
        _extractor(tmp_path, exc=FileNotFoundError()).extract(MESSAGES, scope="project:x", capture_hash="h1")
    assert caught.value.reason == "cli_missing"

    monkeypatch.setattr("memorymaster.core.llm_provider._resolve_claude_bin", lambda: None)
    with pytest.raises(ProviderConfigError) as caught:
        ClaudeCliExtractor(work_dir=tmp_path).extract(MESSAGES, scope="project:x", capture_hash="h1")
    assert caught.value.reason == "cli_missing"


def test_malformed_candidates_are_output_errors(tmp_path):
    for raw in ('{"candidates": "nope"}', "not json at all"):
        envelope = json.dumps({"type": "result", "result": raw})
        with pytest.raises(ProviderOutputError):
            _extractor(tmp_path, envelope).extract(MESSAGES, scope="project:x", capture_hash="h1")


@pytest.mark.parametrize(("value", "kind"), [
    ("claude", ClaudeCliExtractor), ("Claude", ClaudeCliExtractor), ("claude_cli", ClaudeCliExtractor),
    ("gemini", GeminiExtractor), ("opencode", OpenCodeExtractor),
])
def test_env_selects_extract_provider(monkeypatch, value, kind):
    monkeypatch.setenv("MEMORYMASTER_DREAM_EXTRACT_PROVIDER", value)
    monkeypatch.setenv("GEMINI_API_KEY", "fixture")
    assert isinstance(create_dream_extractor(), kind)


def test_unknown_extract_provider_message_lists_claude(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_DREAM_EXTRACT_PROVIDER", "nope")
    with pytest.raises(ProviderCallError, match="claude"):
        create_dream_extractor()


def test_dream_extract_claude_does_not_route_capture_llm_to_paid_api(monkeypatch):
    from memorymaster.capture.worker import capture_llm_overrides

    for name in ("MEMORYMASTER_CAPTURE_LLM_PROVIDER", "MEMORYMASTER_LLM_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMORYMASTER_DREAM_EXTRACT_PROVIDER", "claude")
    assert capture_llm_overrides()["MEMORYMASTER_LLM_PROVIDER"] == "claude_cli"


def test_capture_never_queues_the_claude_cli_scratch_sessions(tmp_path, monkeypatch):
    """Recursion guard: an extraction session must not be captured and extracted again."""
    from memorymaster.dreaming.capture import capture_transcript
    from memorymaster.dreaming.ledger import DreamLedger

    scratch = tmp_path / "scratch"
    monkeypatch.setenv("MEMORYMASTER_CLAUDE_CLI_CWD", str(scratch))
    ledger = DreamLedger(tmp_path / "capture.db")
    for cwd in (scratch, scratch / "sub"):
        result = capture_transcript(
            tmp_path / "missing.jsonl", provider="claude", session_id="s", cwd=str(cwd), ledger=ledger,
        )
        assert result == {"queued": 0, "reason": "internal_scratch"}
    # A sibling folder sharing the prefix is a real project, not the scratch dir.
    other = capture_transcript(
        tmp_path / "missing.jsonl", provider="claude", session_id="s", cwd=str(tmp_path / "scratch2"), ledger=ledger,
    )
    assert other.get("reason") != "internal_scratch"


def test_opencode_prompt_is_unchanged_by_the_shared_rules(tmp_path):
    prompt = OpenCodeExtractor._prompt(MESSAGES, "project:x")
    assert prompt.startswith("Extract at most five stable facts, decisions, preferences, profiles, or constraints.")
    assert "implementation progress. Return one JSON object with a candidates array." in prompt
    assert prompt.count("Output JSON only.") == 1


def test_claude_extractor_is_a_consolidation_fallback(tmp_path):
    """The Beat's extractor is Claude: T-1793's fallback returned None for it, so an agy hang always opened the breaker."""
    from memorymaster.dreaming.models import DreamCandidate
    from memorymaster.dreaming.providers import consolidation_fallback

    seen = []
    raw = json.dumps({"decisions": [{"candidate_id": "c1", "action": "ignore", "rationale": "fixture", "confidence": .9}]})
    envelope = json.dumps({"type": "result", "result": raw, "usage": {"input_tokens": 11, "output_tokens": 3}})
    fallback = consolidation_fallback(_extractor(tmp_path, envelope, seen=seen))
    candidate = DreamCandidate("c1", "Prefer readable code", "preference", "user", "prefers",
                               "readable code", "personal", "m1", "Prefer readable code", .9)

    result = fallback.consolidate([candidate], [], scope="personal")

    assert fallback is not None and fallback.uncapped_daily is True
    assert result.decisions[0].candidate_id == "c1"
    assert (result.usage.provider, result.usage.model) == ("claude", "haiku")
    assert (result.usage.input_tokens, result.usage.output_tokens) == (11, 3)
    assert seen[0][4]["MEMORYMASTER_INTERNAL_RUN"] == "1"  # the headless child must not be recaptured
