"""Build and validate local human-review packets from frozen Dreaming cohorts."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from memorymaster.dreaming.cohort_evaluation import evaluate_cohort
from memorymaster.dreaming.evaluation import ACTIVATION_THRESHOLDS
from memorymaster.dreaming.review_cohort import decision_template, verify_sources


PACKET_SCHEMA = "memorymaster.human-review-packet.v1"


def build_review_packet(
    manifest: dict[str, Any], sources: list[dict[str, Any]], ai_labels: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return emitted, source-evidenced decisions suitable for local human review."""
    _validate_manifest(manifest)
    verify_sources(manifest, sources)
    templates = _templates(manifest, sources)
    labels = _validated_ai_labels(manifest, sources, ai_labels, templates)
    by_source = {int(source["capture_id"]): source for source in sources}
    emitted = [label for label in labels if label["emitted"]]
    records = [
        _packet_record(label, by_source[int(label["capture_id"])], templates[label["record_id"]])
        for label in emitted
    ]
    return {
        "schema": PACKET_SCHEMA,
        "cohort_version": manifest["cohort_version"],
        "cohort_fingerprint": manifest["sources_fingerprint"],
        "population": manifest["population"],
        "selected_captures": len(sources),
        "total_decisions": len(templates),
        "emitted_decisions": len(records),
        "required_human_reviews": ACTIVATION_THRESHOLDS["minimum_human_reviews"],
        "records": records,
    }


def validate_human_reviews(
    packet: dict[str, Any], records: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate a partial local export without treating it as a new evaluator."""
    packet_by_id = _packet_records(packet)
    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in records:
        accepted.append(_validate_human_review(row, packet, packet_by_id, seen))
    return accepted


def _packet_records(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if packet.get("schema") != PACKET_SCHEMA:
        raise ValueError("unknown human review packet schema")
    fingerprint = packet.get("cohort_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("packet cohort fingerprint is required")
    packet_records = packet.get("records")
    if not isinstance(packet_records, list):
        raise ValueError("packet records must be an array")
    packet_by_id = {
        item.get("record_id"): item for item in packet_records if isinstance(item, dict)
    }
    if len(packet_by_id) != len(packet_records) or not all(
        isinstance(item, str) and item for item in packet_by_id
    ):
        raise ValueError("packet record ids must be unique strings")
    return packet_by_id


def _validate_human_review(
    row: dict[str, Any], packet: dict[str, Any], packet_by_id: dict[str, dict[str, Any]], seen: set[str],
) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("human review must be a JSON object")
    record_id = row.get("record_id")
    if not isinstance(record_id, str) or record_id not in packet_by_id or record_id in seen:
        raise ValueError("human review record_id is unknown or duplicated")
    packet_record = packet_by_id[record_id]
    if type(row.get("capture_id")) is not int or row["capture_id"] != packet_record.get("capture_id"):
        raise ValueError("human review capture_id does not match packet")
    if row.get("cohort_fingerprint") != packet["cohort_fingerprint"] or row.get("label_origin") != "human":
        raise ValueError("human review fingerprint and provenance are required")
    if type(row.get("human_accept")) is not bool:
        raise ValueError("human review must be accepted or rejected")
    if not isinstance(row.get("reviewer"), str) or not row["reviewer"].strip():
        raise ValueError("human reviewer is required")
    if not isinstance(row.get("rationale"), str) or not row["rationale"].strip():
        raise ValueError("human rationale is required")
    for field in ("semantic_sufficiency", "current_validity", "useful", "scope_affirmed"):
        if type(row.get(field)) is not bool:
            raise ValueError(f"human review {field} must be yes or no")
    if row.get("should_emit") is not row["useful"]:
        raise ValueError("human review should_emit must equal useful")
    if row.get("actual_scope") != packet_record.get("actual_scope"):
        raise ValueError("human review actual_scope must match packet")
    expected_scope = row.get("expected_scope")
    if not isinstance(expected_scope, str) or not expected_scope.strip():
        raise ValueError("human review expected_scope is required")
    if row["scope_affirmed"] != (expected_scope == row["actual_scope"]):
        raise ValueError("human review scope affirmation does not match expected_scope")
    seen.add(record_id)
    return dict(row)


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if not isinstance(manifest, dict):
        raise ValueError("cohort manifest must be an object")
    for key in ("cohort_version", "sources_fingerprint"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise ValueError(f"cohort manifest {key} is required")
    if type(manifest.get("population")) is not int or manifest["population"] < 0:
        raise ValueError("cohort manifest population must be a non-negative integer")


def _templates(manifest: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    templates: dict[str, dict[str, Any]] = {}
    for source in sources:
        if not isinstance(source, dict) or type(source.get("capture_id")) is not int:
            raise ValueError("frozen source capture_id must be an integer")
        candidates = source.get("candidates")
        if not isinstance(candidates, list):
            raise ValueError("frozen source candidates must be an array")
        for candidate in candidates or [None]:
            template = decision_template(source, candidate, manifest)
            record_id = template["record_id"]
            if record_id in templates:
                raise ValueError("duplicate frozen decision record")
            templates[record_id] = template
    return templates


def _validated_ai_labels(
    manifest: dict[str, Any], sources: list[dict[str, Any]], labels: list[dict[str, Any]],
    templates: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(labels, list):
        raise ValueError("AI labels must be an array")
    if any(not isinstance(label, dict) for label in labels):
        raise ValueError("AI labels must be JSON objects")
    report = evaluate_cohort(manifest, sources, labels)
    if report["cohort_errors"] or report["invalid_records"] or report["duplicate_record_ids"]:
        raise ValueError("AI labels do not match the frozen cohort")
    if set(label.get("record_id") for label in labels) != set(templates):
        raise ValueError("AI labels must cover every frozen decision exactly once")
    for label in labels:
        template = templates[label["record_id"]]
        if label.get("label_origin") != "ai":
            raise ValueError("review packet requires AI-provenance labels")
        if label.get("cohort_fingerprint") != manifest["sources_fingerprint"]:
            raise ValueError("AI label fingerprint does not match cohort")
        if type(label.get("capture_id")) is not int or label["capture_id"] != template["capture_id"]:
            raise ValueError("AI label capture_id does not match frozen decision")
        if not isinstance(label.get("candidate_id"), str) or label["candidate_id"] != template["candidate_id"]:
            raise ValueError("AI label candidate_id does not match frozen decision")
        if not isinstance(label.get("rationale"), str) or not label["rationale"].strip():
            raise ValueError("AI label rationale must be non-empty text")
    return labels


def _packet_record(label: dict[str, Any], source: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    candidate_id = label.get("candidate_id")
    candidate = next(
        (item for item in source["candidates"] if isinstance(item, dict) and item.get("candidate_id") == candidate_id),
        None,
    )
    if candidate is None:
        raise ValueError("emitted label has no frozen candidate")
    memory_text = candidate.get("text")
    evidence_id = candidate.get("evidence_message_id")
    evidence_quote = candidate.get("evidence_quote")
    if not all(isinstance(value, str) and value for value in (memory_text, evidence_id, evidence_quote)):
        raise ValueError("emitted candidate is missing source memory or evidence")
    exact = source.get("evidence_exact")
    if not isinstance(exact, dict) or exact.get(candidate_id) is not True:
        raise ValueError("emitted candidate lacks exact frozen evidence")
    evidence = [
        {"message_id": message["message_id"], "text": message["text"]}
        for message in source.get("messages", [])
        if isinstance(message, dict) and message.get("message_id") == evidence_id
        and isinstance(message.get("text"), str) and evidence_quote in message["text"]
    ]
    if len(evidence) != 1:
        raise ValueError("emitted candidate evidence is malformed")
    return {
        "record_id": template["record_id"],
        "capture_id": template["capture_id"],
        "memory_text": memory_text,
        "actual_scope": template["actual_scope"],
        "actual_action": template["actual_action"],
        "evidence": evidence,
        "evidence_quote": evidence_quote,
        "ai_rationale": label["rationale"],
    }


__all__ = ["PACKET_SCHEMA", "build_review_packet", "validate_human_reviews"]
