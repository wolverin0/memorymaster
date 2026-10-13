"""Hit@k, rank and zero-result math of scripts/eval_recall_quality.py.

The retrieval run uses a disposable database (pattern of test_public_demo.py)
and legacy lexical ranking only, so no embedding model is loaded.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "eval_recall_quality.py"


@pytest.fixture(scope="module")
def eval_module():
    spec = importlib.util.spec_from_file_location("eval_recall_quality", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_recall_quality"] = module
    spec.loader.exec_module(module)
    return module


def test_hit_at_k_and_rank(eval_module) -> None:
    returned = [10, 20, 30, 40, 50, 60]
    assert eval_module.hit_at_k(returned, [30], 5) is True
    assert eval_module.hit_at_k(returned, [60], 5) is False  # rank 6 is outside k=5
    assert eval_module.hit_at_k(returned, [60], 6) is True
    assert eval_module.hit_at_k(returned, [99, 20], 1) is False
    assert eval_module.hit_at_k([], [10], 5) is False
    assert eval_module.target_rank(returned, [99, 40, 20]) == 2  # best-placed target
    assert eval_module.target_rank(returned, [99]) is None


def test_percentile_is_nearest_rank(eval_module) -> None:
    values = [float(v) for v in range(1, 11)]
    assert eval_module.percentile(values, 50) == 5.0
    assert eval_module.percentile(values, 95) == 10.0
    assert eval_module.percentile([7.0], 95) == 7.0
    assert eval_module.percentile([], 50) is None


def test_summarize_math(eval_module) -> None:
    rows = [
        {"rank": 1, "returned": 4, "latency_ms": 10.0},
        {"rank": 5, "returned": 6, "latency_ms": 20.0},
        {"rank": 8, "returned": 8, "latency_ms": 30.0},  # found, outside k=5
        {"rank": None, "returned": 2, "latency_ms": 40.0},  # results, wrong ones
        {"rank": None, "returned": 0, "latency_ms": 100.0},  # zero-result
    ]
    summary = eval_module.summarize(rows, 5)
    assert summary["n"] == 5
    assert summary["hit_at_5"] == 0.4
    assert summary["hit_at_any"] == 0.6
    assert summary["zero_result_rate"] == 0.2
    assert summary["mrr"] == round((1 + 1 / 5 + 1 / 8) / 5, 3)
    assert summary["mean_results"] == 4.0
    assert summary["latency_ms_p50"] == 30.0
    assert summary["latency_ms_p95"] == 100.0
    assert eval_module.summarize([], 5) == {"n": 0}


def test_legacy_retrieval_on_disposable_db(eval_module, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MEMORYMASTER_EMBEDDING_PROVIDER", "hash")
    db = tmp_path / "eval.db"
    service = MemoryService(db, workspace_root=tmp_path)
    service.init_db()
    texts = {
        "pelican": "Pelicans nest on the limestone cliffs beside the harbour lighthouse.",
        "kiln": "The ceramic kiln must be preheated to nine hundred degrees before glazing.",
        "ferry": "The ferry timetable changes to winter hours on the first of November.",
    }
    ids: dict[str, int] = {}
    for key, text in texts.items():
        claim = service.ingest(
            text,
            [CitationInput(source="test", locator=key)],
            scope="project:evalset",
            confidence=0.9,
            source_agent="test-eval",
        )
        confirmed = service.store.apply_status_transition(
            claim, to_status="confirmed", reason="fixture", event_type="validator"
        )
        ids[key] = confirmed.id

    items = [
        {"id": "q1", "provenance": "claim-derived", "question": "pelicans harbour lighthouse",
         "scope": ["project:evalset"], "target_claim_ids": [ids["pelican"]]},
        {"id": "q2", "provenance": "claim-derived", "question": "kiln preheated glazing",
         "scope": None, "target_claim_ids": [ids["kiln"]]},
        {"id": "q3", "provenance": "transcript", "question": "ferry winter timetable",
         "scope": ["project:evalset"], "target_claim_ids": [ids["kiln"]]},  # wrong target on purpose
        {"id": "q4", "provenance": "transcript", "question": "zeppelin",
         "scope": ["project:evalset"], "target_claim_ids": [ids["ferry"]]},  # nothing matches
        {"id": "q5", "provenance": "transcript", "question": "pelicans harbour lighthouse",
         "scope": ["project:elsewhere"], "target_claim_ids": [ids["pelican"]]},  # scope excludes it
    ]
    rows = eval_module.evaluate(service, items, "legacy", 5)

    by_id = {row["id"]: row for row in rows}
    assert by_id["q1"]["rank"] == 1 and by_id["q1"]["hit"] is True
    assert by_id["q2"]["rank"] == 1 and by_id["q2"]["hit"] is True
    assert by_id["q3"]["hit"] is False and by_id["q3"]["returned"] >= 1
    assert by_id["q4"]["returned"] == 0 and by_id["q4"]["rank"] is None
    assert by_id["q5"]["returned"] == 0
    assert all(row["effective_mode"] == "legacy" for row in rows)

    transcript = eval_module.summarize([r for r in rows if r["provenance"] == "transcript"], 5)
    assert transcript["hit_at_5"] == 0.0
    assert transcript["zero_result_rate"] == round(2 / 3, 3)
    derived = eval_module.summarize([r for r in rows if r["provenance"] == "claim-derived"], 5)
    assert derived["hit_at_5"] == 1.0 and derived["zero_result_rate"] == 0.0

    # CLI: JSON on stdout, per-question results written, database untouched by reads.
    questions = tmp_path / "questions.json"
    questions.write_text(json.dumps({"items": items}), encoding="utf-8")
    out = tmp_path / "results.json"
    size_before = db.stat().st_size
    assert eval_module.main(
        ["--mode", "legacy", "--set", str(questions), "--db", str(db), "--out", str(out)]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["config"]["store_read_only"] is True
    assert payload["by_provenance"]["claim-derived"]["hit_at_5"] == 1.0
    assert payload["by_provenance"]["transcript"]["zero_result_rate"] == round(2 / 3, 3)
    assert len(json.loads(out.read_text(encoding="utf-8"))["items"]) == 5
    assert db.stat().st_size == size_before
