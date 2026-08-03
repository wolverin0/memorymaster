"""LongMemEval OpenCode OAuth judge routing and provenance contracts."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("huggingface_hub")

_SPEC = importlib.util.spec_from_file_location(
    "bench_longmemeval_opencode",
    Path(__file__).resolve().parent / "bench_longmemeval.py",
)
assert _SPEC and _SPEC.loader
bench = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = bench
_SPEC.loader.exec_module(bench)


class _Judge:
    def complete(self, prompt: str):
        return SimpleNamespace(
            text="answer",
            model="openai/gpt-5.4-mini",
            provider="openai",
            output_tokens=7,
            provenance=lambda: {
                "provider": "openai",
                "model": "openai/gpt-5.4-mini",
                "effort": "medium",
                "opencode_version": "1.2.3",
                "prompt_hash": "abc",
                "latency_ms": 5,
            },
        )


def test_opencode_judge_is_keyless_single_provider_and_records_provenance() -> None:
    client = bench.JudgeClient(
        anthropic_api_key="",
        openai_api_key="",
        gemini_api_key="",
        primary="opencode",
        pacing_seconds=0,
        opencode_judge=_Judge(),
    )

    result = client.complete("question", max_tokens=10)

    assert client.provider_order == ["opencode"]
    assert result.text == "answer"
    assert result.model == "openai/gpt-5.4-mini"
    assert client.judge_used_label == "opencode"
    assert client.call_provenance == [
        {
            "provider": "openai",
            "model": "openai/gpt-5.4-mini",
            "effort": "medium",
            "opencode_version": "1.2.3",
            "prompt_hash": "abc",
            "latency_ms": 5,
        }
    ]


def test_keyless_opencode_run_is_not_rejected_as_missing_api_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    payload = bench.run_full([], [], judge_name="opencode", max_seconds=1)

    assert payload["judge_primary"] == "opencode"
    assert payload["judge_config"]["model"] == "openai/gpt-5.4-mini"
    assert payload["judge_provenance"] == []


def test_cli_accepts_explicit_opencode_model_and_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bench",
            "--retrieval-only",
            "--judge",
            "opencode",
            "--judge-model",
            "openai/gpt-5.4-mini",
            "--judge-effort",
            "medium",
        ],
    )

    args = bench.parse_args()

    assert args.judge == "opencode"
    assert args.judge_model == "openai/gpt-5.4-mini"
    assert args.judge_effort == "medium"
