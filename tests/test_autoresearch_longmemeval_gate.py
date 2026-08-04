from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "autoresearch_longmemeval_gate.py"


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("autoresearch_longmemeval_gate", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _payload(question_ids: list[str]) -> dict[str, object]:
    return {
        "retrieval": {
            "results": [
                {
                    "question_id": question_id,
                    "top_session_ids": [f"session-{question_id}"],
                    "reciprocal_rank": 1.0,
                    "recall_at_5": True,
                    "recall_at_10": True,
                }
                for question_id in question_ids
            ]
        }
    }


def test_signature_selects_deterministic_held_out_window() -> None:
    gate = _load_gate_module()
    payload = _payload(["q0", "q1", "q2", "q3"])

    signature = gate._signature(payload, offset=2, limit=2)

    assert [row[0] for row in signature] == ["q2", "q3"]


def test_run_benchmark_forwards_offset(monkeypatch, tmp_path: Path) -> None:
    gate = _load_gate_module()
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs

    monkeypatch.setattr(gate.subprocess, "run", fake_run)

    gate._run_benchmark(tmp_path / "result.json", limit=25, offset=50)

    command = captured["command"]
    assert command[command.index("--offset") + 1] == "50"
    assert command[command.index("--limit") + 1] == "25"


def test_persist_gate_summary_exposes_metrics_without_losing_evidence(
    tmp_path: Path,
) -> None:
    gate = _load_gate_module()
    output = tmp_path / "result.json"
    payload = {"retrieval": {"metrics": {"mrr": 0.9}}, "questions": 25}
    summary = {
        "mrr": 0.9,
        "recall_at_5": 1.0,
        "provider_calls": 0,
        "questions": 25,
    }

    gate._persist_gate_summary(output, payload, summary)

    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted["mrr"] == 0.9
    assert persisted["recall_at_5"] == 1.0
    assert persisted["provider_calls"] == 0
    assert persisted["retrieval"] == payload["retrieval"]
