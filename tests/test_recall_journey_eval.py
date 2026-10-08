from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import scripts.eval_recall_journey as evaluator
from scripts.eval_recall_journey import DEFAULT_FIXTURE, run


def test_disposable_recall_journey_observes_governed_delivery(tmp_path: Path) -> None:
    output = tmp_path / "journey.json"
    result = run(DEFAULT_FIXTURE, output)

    assert json.loads(output.read_text(encoding="utf-8")) == result
    assert result["fixture_sha256"] == hashlib.sha256(
        DEFAULT_FIXTURE.read_bytes()
    ).hexdigest()
    assert result["jev_surfaces"] == "off"
    assert result["provider_call_count"] == 0
    assert result["monetary_cost"].startswith("0 observed provider calls")
    assert result["source_context_hook"] == "memorymaster/recall/context_hook.py"

    summary = result["summary"]
    assert summary["cases_total"] == 5
    assert summary["cases_without_error"] == 5
    assert summary["answer_cases_total"] == 4
    assert summary["candidate_coverage"] == 1.0
    assert summary["ranked_hit_at_5"] == 1.0
    assert summary["rendered_hit_at_5"] == 1.0
    assert summary["ranked_precision_at_5_fixed"] == 1 / 5
    assert summary["rendered_precision_at_5_fixed"] == 1 / 5
    assert summary["answer_present_rate"] == 1.0
    assert summary["forbidden_exclusion_rate"] == 1.0
    assert summary["citation_source_correct_rate"] == 1.0
    assert summary["budget_chars_estimate_min"] == 640
    assert summary["budget_chars_estimate_max"] == 800

    retired = next(case for case in result["cases"] if case["key"] == "retired_support")
    assert retired["ranked_hit_at_5"] is None
    assert retired["rendered_hit_at_5"] is None
    assert retired["expected_answer_present_in_decoded_delivery"] is None
    assert retired["forbidden_in_rendered"] is False

    oversized = next(case for case in result["cases"] if case["key"] == "oversized_render")
    assert oversized["allowed_overflow_rendered"] is False
    assert oversized["allowed_overflow_in_candidates"] is True
    assert oversized["allowed_overflow_in_ranked"] is True
    assert oversized["forbidden_in_rendered"] is False

    tail = next(case for case in result["cases"] if case["key"] == "long_tail_unicode")
    assert tail["rendered_hit_at_5"] is True
    assert tail["expected_answer_present_in_decoded_delivery"] is True
    assert tail["unicode_tail_present_in_decoded_delivery"] is True


def test_failed_positive_case_is_counted_as_failure(tmp_path: Path, monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise TimeoutError("synthetic evaluator failure")

    monkeypatch.setattr(evaluator.context_hook, "recall", fail)
    result = evaluator._run_case(
        {"key": "positive", "query": "x", "expected": ["answer"]},
        {"answer": 7},
        SimpleNamespace(store=SimpleNamespace(db_path=tmp_path / "disposable.db")),
        tmp_path,
        {"key": "positive", "query": "x", "expected": ["answer"]},
        [],
    )
    assert result["error"].startswith("TimeoutError:")
    assert result["expected_ids"] == [7]
    assert result["ranked_hit_at_5"] is False
    assert result["ranked_precision_at_5_fixed"] == 0.0
    assert result["expected_answer_present_in_decoded_delivery"] is False


def test_empty_expected_case_is_unmeasured_and_citation_mismatch_fails(monkeypatch) -> None:
    monkeypatch.setattr(evaluator.context_hook, "recall", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("synthetic")))
    result = evaluator._run_case(
        {"key": "negative", "query": "x", "expected": []},
        {"answer": 7},
        SimpleNamespace(store=SimpleNamespace(db_path="disposable.db")),
        Path("."),
        {"key": "negative", "query": "x", "expected": []},
        [],
    )
    assert result["error"].startswith("TimeoutError:")
    assert result["expected_ids"] == []
    assert result["ranked_hit_at_5"] is None
    assert evaluator._citation_check([7], {"7": ["wrong"]}, {"7": "expected"}) == (1, 0, False)


def test_p95_uses_nearest_rank() -> None:
    assert evaluator._nearest_rank_p95([1.0, 2.0, 3.0]) == 3.0


def test_installed_mode_preserves_interpreter_import_order(monkeypatch) -> None:
    original_path = list(evaluator.sys.path)
    monkeypatch.setattr(evaluator.sys, "path", list(original_path))

    evaluator._configure_import_path(
        "C:/Python/Lib/site-packages", installed_mode=True
    )

    assert evaluator.sys.path == original_path
    assert "C:/Python/Lib/site-packages" not in evaluator.sys.path
