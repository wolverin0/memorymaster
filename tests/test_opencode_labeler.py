"""OpenCode OAuth labeler provenance, resume, and failure contracts."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from memorymaster.evaluation.opencode_judge import OpenCodeJudgeError

_SPEC = importlib.util.spec_from_file_location(
    "label_prompts_with_judge",
    Path(__file__).resolve().parents[1] / "scripts" / "label_prompts_with_judge.py",
)
assert _SPEC and _SPEC.loader
labeler = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = labeler
_SPEC.loader.exec_module(labeler)


class _Judge:
    def complete(self, prompt: str):
        return SimpleNamespace(
            text="[12, 999]",
            provenance=lambda: {
                "provider": "openai",
                "model": "openai/gpt-5.4-mini",
                "effort": "medium",
                "opencode_version": "1.2.3",
                "prompt_hash": "abc",
                "latency_ms": 5,
            },
        )


def test_call_judge_returns_filtered_ids_and_opencode_provenance() -> None:
    ids, provenance = labeler._call_judge(
        "query",
        [{"id": 12, "text": "answer"}],
        provider="opencode",
        judge=_Judge(),
    )

    assert ids == [12, 999]
    assert provenance["model"] == "openai/gpt-5.4-mini"
    assert provenance["effort"] == "medium"


def test_main_resumes_without_rejudging_existing_label(
    tmp_path: Path, monkeypatch
) -> None:
    prompts = tmp_path / "prompts.jsonl"
    prompts.write_text(json.dumps({"text": "existing query"}) + "\n", encoding="utf-8")
    output = tmp_path / "labels.json"
    sha = labeler._sha1_16("existing query")
    output.write_text(json.dumps({"labels": {sha: [12]}}), encoding="utf-8")
    monkeypatch.setattr(
        labeler,
        "_get_candidates",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must resume")),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "labeler",
            "--prompts",
            str(prompts),
            "--db",
            str(tmp_path / "unused.db"),
            "--labels-out",
            str(output),
            "--judge-provider",
            "opencode",
        ],
    )

    assert labeler.main() == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["labels"] == {sha: [12]}
    assert payload["judge"]["provider"] == "opencode"
    assert len(payload["fixture"]["prompts_sha256"]) == 64


def test_provider_error_never_becomes_an_empty_ground_truth_label(
    tmp_path: Path, monkeypatch
) -> None:
    prompts = tmp_path / "prompts.jsonl"
    prompts.write_text(json.dumps({"text": "new query"}) + "\n", encoding="utf-8")
    output = tmp_path / "labels.json"

    class _FailingJudge:
        def complete(self, prompt: str):
            raise OpenCodeJudgeError("timeout", "timed out")

    monkeypatch.setattr(labeler, "OpenCodeJudge", lambda **kwargs: _FailingJudge())
    monkeypatch.setattr(
        labeler,
        "_get_candidates",
        lambda *args, **kwargs: [{"id": 12, "text": "candidate"}],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "labeler",
            "--prompts",
            str(prompts),
            "--db",
            str(tmp_path / "unused.db"),
            "--labels-out",
            str(output),
            "--judge-provider",
            "opencode",
        ],
    )

    assert labeler.main() == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    sha = labeler._sha1_16("new query")
    assert sha not in payload["labels"]
    assert payload["errors"][sha]["code"] == "timeout"
