"""Bind the existing Dreaming evaluator to immutable source-cohort evidence."""

from memorymaster.dreaming.evaluation import evaluate_records
from memorymaster.dreaming.review_cohort import decision_template, verify_sources


JUDGMENTS = {"should_emit", "expected_scope", "expected_action", "semantic_sufficiency", "current_validity",
             "useful", "rationale", "human_accept", "label_origin", "reviewer"}


def evaluate_cohort(manifest: dict, sources: list[dict], labels: list[dict], human_labels: list[dict] = ()) -> dict:
    verify_sources(manifest, sources)
    templates = {item["record_id"]: item for source in sources for candidate in (source["candidates"] or [None])
                 for item in [decision_template(source, candidate, manifest)]}
    by_id = {row["record_id"]: row for row in labels}
    errors = _label_errors(manifest, sources, labels, templates)
    if len(by_id) != len(labels):
        errors.append("duplicate_ai_labels")
    reviewed_ids = set()
    for human in human_labels:
        record_id = human.get("record_id")
        if (record_id not in by_id or record_id in reviewed_ids or human.get("label_origin") != "human"
                or type(human.get("human_accept")) is not bool
                or not human.get("reviewer")
                or not isinstance(human.get("rationale"), str) or not human["rationale"].strip()
                or human.get("cohort_fingerprint") != manifest["sources_fingerprint"]):
            errors.append("invalid_human_override")
            continue
        reviewed_ids.add(record_id)
        by_id[record_id] = {**by_id[record_id], **{key: value for key, value in human.items() if key in JUDGMENTS}}
    report = evaluate_records(list(by_id.values()))
    missing = sorted(set(templates) - set(by_id))
    if missing:
        errors.append("missing_source_decisions")
    return {**report, "cohort_version": manifest["cohort_version"], "cohort_fingerprint": manifest["sources_fingerprint"],
            "cohort_errors": sorted(set(errors)), "missing_records": missing,
            "activation_ready": report["activation_ready"] and not errors,
            "acceptance": "ACCEPTED" if report["activation_ready"] and not errors else "PENDING",
            "provider_usage": manifest["provider_usage"]}


def _label_errors(manifest, sources, labels, templates):
    errors = []
    capture_ids = {source["capture_id"] for source in sources}
    for row in labels:
        if row.get("cohort_fingerprint") != manifest["sources_fingerprint"] or row.get("capture_id") not in capture_ids:
            errors.append("cohort_binding_mismatch")
        if row.get("label_origin") not in {"ai", "human"} or not row.get("rationale"):
            errors.append("missing_label_provenance_or_rationale")
        original = templates.get(row.get("record_id"))
        if original:
            for name in ("emitted", "actual_action", "actual_scope", "structured_valid", "evidence_exact"):
                if row.get(name) != original[name]:
                    errors.append("observed_outcome_changed")
        elif not (str(row.get("record_id", "")).startswith(f"capture-{row.get('capture_id')}:omission:")
                  and row.get("emitted") is False):
            errors.append("unknown_decision")
    return errors
