"""Evaluation-only OpenCode OAuth judge command and provenance contracts."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from memorymaster.evaluation.opencode_judge import OpenCodeJudge, OpenCodeJudgeError


def test_opencode_judge_is_headless_keyless_and_records_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], str, dict[str, str]]] = []

    def runner(command, prompt, timeout, cwd, env):
        calls.append((command, prompt, env))
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "opencode 1.2.3\n", "")
        if command[1:3] == ["session", "delete"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        events = [
            {
                "type": "text",
                "sessionID": "judge-session",
                "part": {"text": "[12]"},
            },
            {
                "type": "step_finish",
                "part": {"tokens": {"input": 9, "output": 3}},
            },
        ]
        return subprocess.CompletedProcess(
            command, 0, "\n".join(json.dumps(event) for event in events), ""
        )

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")
    monkeypatch.setenv("MEMORYMASTER_OPENCODE_AUTH_MODE", "oauth")
    judge = OpenCodeJudge(command="opencode", runner=runner, work_dir=tmp_path)

    result = judge.complete("strict evaluation prompt")

    assert result.text == "[12]"
    assert result.provider == "openai"
    assert result.model == "openai/gpt-5.4-mini"
    assert result.effort == "medium"
    assert result.opencode_version == "opencode 1.2.3"
    assert result.input_tokens == 9
    assert result.output_tokens == 3
    assert result.prompt_hash == hashlib.sha256(
        b"strict evaluation prompt"
    ).hexdigest()
    assert result.latency_ms >= 0
    assert "OPENAI_API_KEY" not in calls[1][2]
    assert calls[1][0] == [
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
    assert calls[2][0] == ["opencode", "session", "delete", "judge-session"]


def test_opencode_judge_rejects_malformed_event_output(tmp_path: Path) -> None:
    def runner(command, prompt, timeout, cwd, env):
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "1.2.3", "")
        return subprocess.CompletedProcess(command, 0, "not-json", "")

    judge = OpenCodeJudge(command="opencode", runner=runner, work_dir=tmp_path)

    with pytest.raises(OpenCodeJudgeError) as exc:
        judge.complete("prompt")
    assert exc.value.code == "malformed_output"


def test_opencode_judge_surfaces_timeout(tmp_path: Path) -> None:
    def runner(command, prompt, timeout, cwd, env):
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "1.2.3", "")
        raise subprocess.TimeoutExpired(command, timeout)

    judge = OpenCodeJudge(command="opencode", runner=runner, work_dir=tmp_path)

    with pytest.raises(OpenCodeJudgeError) as exc:
        judge.complete("prompt")
    assert exc.value.code == "timeout"
