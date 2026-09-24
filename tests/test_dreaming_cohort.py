"""Frozen cohorts include omissions, preserve label provenance and never mutate ledgers."""

import json
import runpy

import pytest

from memorymaster.dreaming.cohort_evaluation import evaluate_cohort
from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.review_cohort import _fingerprint, decision_template, freeze_cohort, verify_sources, write_cohort

H = runpy.run_path("tests/test_dreaming_worker.py")


def _cohort(tmp_path):
    ledger = DreamLedger(tmp_path / "fixture.db")
    capture_id = H["_capture"](ledger)
    ledger.set_extraction(capture_id, [], "fixture")
    return ledger, freeze_cohort(ledger.db_path, since="2026-07-21T00:00:00Z", until="2026-07-22T00:00:00Z",
                                 previous_until="2026-07-21T00:00:00Z", version="fixture-v1")


def _emitted_cohort():
    source = {"capture_id": 1, "scope": "project:test",
              "candidates": [{"candidate_id": "candidate-1", "scope_class": "project"}],
              "applications": [{"candidate_id": "candidate-1", "action": "add"}],
              "evidence_exact": {"candidate-1": True}}
    manifest = {"cohort_version": "fixture-v1", "sources_fingerprint": _fingerprint([source]),
                "provider_usage": {}}
    template = decision_template(source, source["candidates"][0], manifest)
    label = {**template, "label_origin": "ai", "should_emit": True,
             "expected_scope": "project:test", "expected_action": "add", "rationale": "AI rationale"}
    return manifest, [source], [label]


def test_frozen_sample_preserves_ledger_and_detects_tampering(tmp_path):
    ledger, (manifest, sources) = _cohort(tmp_path)
    original = ledger.get_capture(sources[0]["capture_id"])
    assert manifest["population"] == 1
    assert manifest["selected_by_stratum"]["zero_candidates"] == 1
    assert manifest["sampling_shortfalls"]["candidates_no_actions"] == 20
    write_cohort(tmp_path / "cohort", manifest, sources)
    with pytest.raises(FileExistsError):
        write_cohort(tmp_path / "cohort", manifest, sources)
    verify_sources(manifest, sources)
    changed = [{**sources[0], "scope": "project:other"}]
    with pytest.raises(ValueError, match="fingerprint"):
        verify_sources(manifest, changed)
    assert ledger.get_capture(sources[0]["capture_id"]) == original


def test_overlapping_prior_window_is_rejected_before_read(tmp_path):
    with pytest.raises(ValueError, match="overlaps"):
        freeze_cohort(tmp_path / "absent.db", since="2026-07-20T00:00:00Z", until="2026-07-22T00:00:00Z",
                      previous_until="2026-07-21T00:00:00Z", version="v1")
    assert not (tmp_path / "absent.db").exists()


def test_ai_preparation_cannot_supply_human_acceptance_or_drop_negatives(tmp_path):
    _ledger, (manifest, sources) = _cohort(tmp_path)
    template = decision_template(sources[0], None, manifest)
    label = {**template, "label_origin": "ai", "should_emit": False, "rationale": "Routine negative control.",
             "human_accept": True, "semantic_sufficiency": False, "current_validity": False, "useful": False}
    result = evaluate_cohort(manifest, sources, [label])
    assert result["human_reviews"] == 0 and result["acceptance"] == "PENDING"
    assert result["assessment_dimensions"]["semantic_sufficiency"]["positive"] == 0
    missing = evaluate_cohort(manifest, sources, [])
    assert missing["missing_records"] == [template["record_id"]]
    changed = evaluate_cohort(manifest, sources, [{**label, "emitted": True}])
    assert "observed_outcome_changed" in changed["cohort_errors"]
    human = {"record_id": label["record_id"], "label_origin": "human", "human_accept": True,
             "reviewer": "operator", "cohort_fingerprint": "wrong"}
    assert "invalid_human_override" in evaluate_cohort(manifest, sources, [label], [human])["cohort_errors"]


@pytest.mark.parametrize("human_accept", [True, False])
@pytest.mark.parametrize("rationale", ["missing", None, "", "   ", 7, ["reason"], {"text": "reason"}])
def test_human_reviews_require_nonempty_rationale(human_accept, rationale):
    manifest, sources, labels = _emitted_cohort()
    human = {"record_id": labels[0]["record_id"], "label_origin": "human", "reviewer": "operator",
             "human_accept": human_accept, "cohort_fingerprint": manifest["sources_fingerprint"]}
    if rationale != "missing":
        human["rationale"] = rationale

    result = evaluate_cohort(manifest, sources, labels, [human])

    assert "invalid_human_override" in result["cohort_errors"]
    assert result["human_reviews"] == 0
    assert result["label_origins"] == {"ai": 1}


@pytest.mark.parametrize("human_accept", [True, False])
def test_human_review_with_rationale_preserves_acceptance(human_accept):
    manifest, sources, labels = _emitted_cohort()
    human = {"record_id": labels[0]["record_id"], "label_origin": "human", "reviewer": "operator",
             "human_accept": human_accept, "rationale": "Reviewed the cited source and decision.",
             "cohort_fingerprint": manifest["sources_fingerprint"]}

    result = evaluate_cohort(manifest, sources, labels, [human])

    assert result["cohort_errors"] == []
    assert result["human_reviews"] == 1
    assert result["metrics"]["human_acceptance"] == (1.0 if human_accept else 0.0)


def test_review_source_text_is_scrubbed(tmp_path):
    _ledger, (manifest, sources) = _cohort(tmp_path)
    from memorymaster.dreaming.review_cohort import _scrub

    raw = "password=SuperSecret123 user@host.example C:\\Users\\fixture\\private.txt\n10.1.2.3"
    scrubbed = _scrub(raw)
    assert "SuperSecret123" not in scrubbed and "private.txt" not in scrubbed and "10.1.2.3" not in scrubbed
    assert "password" not in json.dumps(manifest)
