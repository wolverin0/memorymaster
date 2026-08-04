from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "autoresearch_longmemeval_qa.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("autoresearch_longmemeval_qa", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _chunk(ids: list[str], correct: list[bool]) -> dict[str, object]:
    results = [
        {
            "question_id": question_id,
            "question_type": "single-session-user",
            "correct": verdict,
        }
        for question_id, verdict in zip(ids, correct, strict=True)
    ]
    return {
        "dataset": "fixture",
        "status": "complete",
        "qa": {
            "mode": "qa-only",
            "judge_model": "openai/gpt-5.4-mini",
            "judge_primary": "opencode",
            "judge_config": {"model": "openai/gpt-5.4-mini", "effort": "medium"},
            "judge_provenance": [],
            "judge_retry_policy": "fixture",
            "judge_pacing_seconds": 0,
            "status": "complete",
            "questions": len(ids),
            "requested_questions": len(ids),
            "correct": sum(correct),
            "accuracy": sum(correct) / len(ids),
            "by_question_type": {},
            "results": results,
            "elapsed_seconds": 2.5,
            "tokens": 100,
        },
    }


def test_load_valid_chunk_rejects_wrong_question_window(tmp_path: Path) -> None:
    qa = _load_module()
    path = tmp_path / "chunk.json"
    _write_json(path, _chunk(["q1"], [True]))

    with pytest.raises(ValueError, match="question IDs"):
        qa._load_valid_chunk(
            path,
            ["q2"],
            judge_model="openai/gpt-5.4-mini",
            judge_effort="medium",
        )


def test_aggregate_recomputes_complete_evidence(tmp_path: Path) -> None:
    qa = _load_module()
    dataset = tmp_path / "dataset.json"
    retrieval = tmp_path / "retrieval.json"
    first = tmp_path / "before-000.json"
    second = tmp_path / "before-002.json"
    _write_json(dataset, [{"question_id": f"q{index}"} for index in range(4)])
    _write_json(retrieval, {"retrieval": {"results": []}})
    _write_json(first, _chunk(["q0", "q1"], [True, False]))
    _write_json(second, _chunk(["q2", "q3"], [True, True]))

    payload = qa._aggregate(
        [(first, qa._read_json(first)), (second, qa._read_json(second))],
        dataset=dataset,
        retrieval_results=retrieval,
        expected_ids=["q0", "q1", "q2", "q3"],
    )

    assert payload["status"] == "complete"
    assert payload["qa"]["questions"] == 4
    assert payload["qa"]["correct"] == 3
    assert payload["qa"]["accuracy"] == 0.75
    assert payload["qa"]["tokens"] == 200
    assert payload["qa"]["elapsed_seconds"] == 5.0
    assert len(payload["qa"]["chunk_evidence"]) == 2


def test_build_command_forwards_exact_chunk_and_judge(tmp_path: Path) -> None:
    qa = _load_module()

    command = qa._build_command(
        python=Path(sys.executable),
        dataset=tmp_path / "dataset.json",
        retrieval_results=tmp_path / "retrieval.json",
        output=tmp_path / "chunk.json",
        offset=20,
        limit=10,
        judge_model="openai/gpt-5.4-mini",
        judge_effort="medium",
        max_seconds=1800,
    )

    assert command[command.index("--offset") + 1] == "20"
    assert command[command.index("--limit") + 1] == "10"
    assert command[command.index("--judge-model") + 1] == "openai/gpt-5.4-mini"
    assert command[command.index("--judge-effort") + 1] == "medium"
