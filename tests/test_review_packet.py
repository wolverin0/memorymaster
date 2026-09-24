"""Review packets preserve frozen sources and permit only valid human exports."""

from __future__ import annotations

import importlib.util
import json
import sys

import pytest

from memorymaster.dreaming.cohort_evaluation import evaluate_cohort
from memorymaster.dreaming.review_cohort import _fingerprint, decision_template
from memorymaster.dreaming.review_packet import build_review_packet, validate_human_reviews


def _inputs():
    source = {
        "capture_id": 7,
        "scope": "project:review",
        "messages": [{"message_id": "m-1", "text": "Use the frozen source sentence as evidence."}],
        "candidates": [{
            "candidate_id": "candidate-7", "text": "The source-backed memory text.",
            "scope_class": "project", "evidence_message_id": "m-1",
            "evidence_quote": "frozen source sentence", "claim_type": "fact",
            "subject": "source", "predicate": "supports", "object_value": "memory",
        }],
        "applications": [{"candidate_id": "candidate-7", "action": "add"}],
        "evidence_exact": {"candidate-7": True},
    }
    manifest = {
        "cohort_version": "review-fixture-v1", "sources_fingerprint": _fingerprint([source]),
        "population": 1, "provider_usage": {},
    }
    template = decision_template(source, source["candidates"][0], manifest)
    label = {
        **template, "label_origin": "ai", "should_emit": True,
        "expected_scope": "project:review", "expected_action": "add",
        "rationale": "The source quote directly supports the decision.",
    }
    return manifest, [source], [label]


def _script_module():
    spec = importlib.util.spec_from_file_location("prepare_dreaming_review", "scripts/prepare_dreaming_review.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_packet_contains_only_actual_emission_and_frozen_evidence():
    manifest, sources, labels = _inputs()
    packet = build_review_packet(manifest, sources, labels)
    assert packet["cohort_fingerprint"] == manifest["sources_fingerprint"]
    assert packet["total_decisions"] == packet["emitted_decisions"] == 1
    assert packet["records"] == [{
        "record_id": labels[0]["record_id"], "capture_id": 7,
        "memory_text": sources[0]["candidates"][0]["text"],
        "actual_scope": "project:review", "actual_action": "add",
        "evidence": [{"message_id": "m-1", "text": sources[0]["messages"][0]["text"]}],
        "evidence_quote": "frozen source sentence",
        "ai_rationale": labels[0]["rationale"],
    }]


@pytest.mark.parametrize("mutate", [
    lambda labels: labels + [dict(labels[0])],
    lambda labels: [],
    lambda labels: [{**labels[0], "cohort_fingerprint": "wrong"}],
    lambda labels: [{**labels[0], "label_origin": "human"}],
])
def test_packet_rejects_duplicate_corrupt_or_non_ai_labels(mutate):
    manifest, sources, labels = _inputs()
    with pytest.raises(ValueError):
        build_review_packet(manifest, sources, mutate(labels))


def test_packet_rejects_missing_or_nonexact_source_evidence():
    manifest, sources, labels = _inputs()
    sources[0]["evidence_exact"] = {"candidate-7": False}
    manifest["sources_fingerprint"] = _fingerprint(sources)
    labels[0]["cohort_fingerprint"] = manifest["sources_fingerprint"]
    with pytest.raises(ValueError):
        build_review_packet(manifest, sources, labels)


def test_packet_rejects_label_rebound_to_a_different_frozen_candidate():
    manifest, sources, labels = _inputs()
    second = {
        **sources[0], "capture_id": 8,
        "messages": [{"message_id": "m-2", "text": "A second frozen source sentence is evidence."}],
        "candidates": [{
            **sources[0]["candidates"][0], "candidate_id": "candidate-8", "evidence_message_id": "m-2",
            "evidence_quote": "second frozen source sentence",
        }],
        "applications": [{"candidate_id": "candidate-8", "action": "add"}],
        "evidence_exact": {"candidate-8": True},
    }
    sources.append(second)
    manifest["sources_fingerprint"] = _fingerprint(sources)
    first = decision_template(sources[0], sources[0]["candidates"][0], manifest)
    second_template = decision_template(second, second["candidates"][0], manifest)
    labels = [
        {**first, "label_origin": "ai", "should_emit": True, "expected_scope": "project:review",
         "expected_action": "add", "rationale": "First source supports the decision."},
        {**second_template, "label_origin": "ai", "should_emit": True, "expected_scope": "project:review",
         "expected_action": "add", "rationale": "Second source supports the decision."},
    ]
    rebound = [{**labels[0], "capture_id": 8, "candidate_id": "candidate-8"}, labels[1]]
    with pytest.raises(ValueError, match="capture_id does not match"):
        build_review_packet(manifest, sources, rebound)


def test_human_export_round_trips_to_existing_evaluator_without_inventing_reviewer():
    manifest, sources, labels = _inputs()
    packet = build_review_packet(manifest, sources, labels)
    human = {
        "record_id": packet["records"][0]["record_id"], "cohort_fingerprint": packet["cohort_fingerprint"],
        "capture_id": 7, "label_origin": "human", "reviewer": "operator", "human_accept": True,
        "rationale": "Accepted after checking the quoted source.",
        "semantic_sufficiency": True, "current_validity": True, "useful": True,
        "should_emit": True, "scope_affirmed": True,
        "actual_scope": "project:review", "expected_scope": "project:review",
    }
    reviews = validate_human_reviews(packet, [human])
    report = evaluate_cohort(manifest, sources, labels, reviews)
    assert report["cohort_errors"] == []
    assert report["human_reviews"] == 1
    assert reviews[0]["reviewer"] == "operator"


@pytest.mark.parametrize("row", [
    {"record_id": "unknown"},
    {"record_id": "capture-7:candidate-7", "label_origin": "human", "human_accept": "unsure"},
    {"record_id": "capture-7:candidate-7", "capture_id": 7, "label_origin": "human", "human_accept": True,
     "reviewer": "operator", "rationale": "ok", "semantic_sufficiency": True, "current_validity": True,
     "useful": True, "should_emit": True, "scope_affirmed": True, "actual_scope": "project:review",
     "expected_scope": "project:other"},
])
def test_human_export_rejects_unknown_unsure_or_malformed_reviews(row):
    manifest, sources, labels = _inputs()
    packet = build_review_packet(manifest, sources, labels)
    row = {"cohort_fingerprint": packet["cohort_fingerprint"], **row}
    with pytest.raises(ValueError):
        validate_human_reviews(packet, [row])


def test_embedded_packet_json_cannot_close_script_tag():
    module = _script_module()
    encoded = module.safe_embedded_json({"text": "</script><script>alert(1)</script>&\u2028\u2029"})
    assert "</script" not in encoded.lower()
    assert "<" not in encoded and ">" not in encoded and "&" not in encoded
    assert "\\u2028" in encoded and "\\u2029" in encoded


def test_local_cli_writes_new_safe_packet_and_html(tmp_path, monkeypatch, capsys):
    manifest, sources, labels = _inputs()
    sources[0]["candidates"][0]["text"] = "</script><script>alert(1)</script>"
    manifest["sources_fingerprint"] = _fingerprint(sources)
    labels[0].update({
        "cohort_fingerprint": manifest["sources_fingerprint"],
        "rationale": "<script>AI rationale</script>",
    })
    cohort = tmp_path / "cohort"
    cohort.mkdir()
    (cohort / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (cohort / "sources.json").write_text(json.dumps(sources), encoding="utf-8")
    labels_path = tmp_path / "ai-labels.jsonl"
    labels_path.write_text(json.dumps(labels[0]) + "\n", encoding="utf-8")
    output = tmp_path / "new-output"
    module = _script_module()
    monkeypatch.setattr(sys, "argv", [
        "prepare_dreaming_review.py", "--cohort", str(cohort), "--ai-labels", str(labels_path),
        "--output", str(output),
    ])

    assert module.main() == 0
    packet = json.loads((output / "packet.json").read_text(encoding="utf-8"))
    html = (output / "review.html").read_text(encoding="utf-8")
    assert packet["records"][0]["memory_text"] == "</script><script>alert(1)</script>"
    assert html.count('<script type="application/json" id="preloaded-packet">') == 1
    assert "__REVIEW_PACKET_JSON__" not in html
    assert "</script><script>alert(1)</script>" not in html
    assert (output / "packet.json").exists() and (output / "review.html").exists()
    assert "memory_text" not in capsys.readouterr().out
