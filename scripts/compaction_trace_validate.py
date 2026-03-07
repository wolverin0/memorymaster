from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def load_json_object(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return raw


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


class Validation:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def add(self, name: str, ok: bool, detail: str, *, severity: str = "error") -> None:
        row = {"name": name, "ok": bool(ok), "severity": severity, "detail": detail}
        self.checks.append(row)
        if ok:
            return
        if severity == "warning":
            self.warnings.append(f"{name}: {detail}")
            return
        self.errors.append(f"{name}: {detail}")

    @property
    def passed(self) -> bool:
        return not self.errors


def _expect_dict(value: Any, label: str, v: Validation) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    v.add(label, False, f"expected object, got {type(value).__name__}")
    return {}


def _expect_list(value: Any, label: str, v: Validation) -> list[Any]:
    if isinstance(value, list):
        return value
    v.add(label, False, f"expected array, got {type(value).__name__}")
    return []


def _citation_node_id_from_any(value: Any) -> str:
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("citation:"):
            return raw
        as_int = to_int(raw, default=-1)
        if as_int >= 0:
            return f"citation:{as_int}"
        return ""
    as_int = to_int(value, default=-1)
    if as_int >= 0:
        return f"citation:{as_int}"
    return ""


def validate_payloads(summary_graph: dict[str, Any], traceability: dict[str, Any]) -> dict[str, Any]:
    v = Validation()

    v.add(
        "summary_graph.artifact_type",
        to_str(summary_graph.get("artifact_type")).strip() == "summary_graph",
        "artifact_type must be 'summary_graph'",
    )
    v.add(
        "traceability.artifact_type",
        to_str(traceability.get("artifact_type")).strip() == "traceability",
        "artifact_type must be 'traceability'",
    )

    run_graph = _expect_dict(summary_graph.get("run"), "summary_graph.run", v)
    run_trace = _expect_dict(traceability.get("run"), "traceability.run", v)
    for key in ("run_id", "retain_days", "event_retain_days", "candidate_claims", "archived_claims", "deleted_events"):
        v.add(
            f"run.{key}.match",
            run_graph.get(key) == run_trace.get(key),
            f"summary_graph.run.{key} must match traceability.run.{key}",
        )

    nodes = _expect_dict(summary_graph.get("nodes"), "summary_graph.nodes", v)
    summary_nodes = _expect_list(nodes.get("summaries"), "summary_graph.nodes.summaries", v)
    claim_nodes = _expect_list(nodes.get("claims"), "summary_graph.nodes.claims", v)
    citation_nodes = _expect_list(nodes.get("citations"), "summary_graph.nodes.citations", v)
    edges = _expect_list(summary_graph.get("edges"), "summary_graph.edges", v)

    trace_summary_rows = _expect_list(traceability.get("summary_to_source"), "traceability.summary_to_source", v)
    trace_lineage_rows = _expect_list(traceability.get("claim_lineage"), "traceability.claim_lineage", v)

    summary_ids: set[str] = set()
    summary_to_claim_ids: dict[str, set[int]] = {}
    summary_to_citation_ids: dict[str, set[str]] = {}
    for idx, row in enumerate(summary_nodes):
        label = f"summary_graph.nodes.summaries[{idx}]"
        row_obj = _expect_dict(row, label, v)
        summary_id = to_str(row_obj.get("id")).strip()
        v.add(f"{label}.id", bool(summary_id), "summary id must be non-empty")
        if summary_id:
            v.add(f"{label}.id.unique", summary_id not in summary_ids, "summary id must be unique")
            summary_ids.add(summary_id)

        claim_ids = _expect_list(row_obj.get("claim_ids"), f"{label}.claim_ids", v)
        claim_id_set = {to_int(item, default=-1) for item in claim_ids if to_int(item, default=-1) >= 0}
        summary_to_claim_ids[summary_id] = claim_id_set
        v.add(
            f"{label}.claim_count",
            to_int(row_obj.get("claim_count"), default=-1) == len(claim_id_set),
            "claim_count must match len(claim_ids)",
        )

        citation_ids = _expect_list(row_obj.get("citation_ids"), f"{label}.citation_ids", v)
        citation_id_set = {to_str(item).strip() for item in citation_ids if to_str(item).strip()}
        summary_to_citation_ids[summary_id] = citation_id_set
        v.add(
            f"{label}.citation_count",
            to_int(row_obj.get("citation_count"), default=-1) == len(citation_id_set),
            "citation_count must match len(citation_ids)",
        )

    claim_ids_seen: set[int] = set()
    claim_to_summary: dict[int, str] = {}
    claim_to_citations: dict[int, set[str]] = {}
    claim_node_ids: set[str] = set()
    for idx, row in enumerate(claim_nodes):
        label = f"summary_graph.nodes.claims[{idx}]"
        row_obj = _expect_dict(row, label, v)
        claim_id = to_int(row_obj.get("claim_id"), default=-1)
        v.add(f"{label}.claim_id", claim_id >= 0, "claim_id must be int >= 0")
        if claim_id >= 0:
            v.add(f"{label}.claim_id.unique", claim_id not in claim_ids_seen, "claim_id must be unique")
            claim_ids_seen.add(claim_id)

        node_id = to_str(row_obj.get("id")).strip()
        expected_node_id = f"claim:{claim_id}" if claim_id >= 0 else ""
        v.add(f"{label}.id", bool(node_id), "claim node id must be non-empty")
        if claim_id >= 0:
            v.add(f"{label}.id.expected", node_id == expected_node_id, f"id should be '{expected_node_id}'")
        if node_id:
            claim_node_ids.add(node_id)

        summary_id = to_str(row_obj.get("summary_id")).strip()
        v.add(
            f"{label}.summary_id",
            bool(summary_id) and summary_id in summary_ids,
            "summary_id must reference an existing summary node",
        )
        if claim_id >= 0 and summary_id:
            claim_to_summary[claim_id] = summary_id

        citation_ids = _expect_list(row_obj.get("citation_ids"), f"{label}.citation_ids", v)
        claim_to_citations[claim_id] = {to_str(item).strip() for item in citation_ids if to_str(item).strip()}

    citation_node_ids: set[str] = set()
    citation_ids_seen: set[int] = set()
    for idx, row in enumerate(citation_nodes):
        label = f"summary_graph.nodes.citations[{idx}]"
        row_obj = _expect_dict(row, label, v)
        citation_id = to_int(row_obj.get("citation_id"), default=-1)
        v.add(f"{label}.citation_id", citation_id >= 0, "citation_id must be int >= 0")
        if citation_id >= 0:
            v.add(f"{label}.citation_id.unique", citation_id not in citation_ids_seen, "citation_id must be unique")
            citation_ids_seen.add(citation_id)

        node_id = to_str(row_obj.get("id")).strip()
        expected_node_id = f"citation:{citation_id}" if citation_id >= 0 else ""
        v.add(f"{label}.id", bool(node_id), "citation node id must be non-empty")
        if citation_id >= 0:
            v.add(f"{label}.id.expected", node_id == expected_node_id, f"id should be '{expected_node_id}'")
        if node_id:
            citation_node_ids.add(node_id)

    summary_to_claim_edges: set[tuple[str, str]] = set()
    claim_to_citation_edges: set[tuple[str, str]] = set()
    for idx, row in enumerate(edges):
        label = f"summary_graph.edges[{idx}]"
        row_obj = _expect_dict(row, label, v)
        edge_type = to_str(row_obj.get("type")).strip()
        from_id = to_str(row_obj.get("from")).strip()
        to_id = to_str(row_obj.get("to")).strip()
        v.add(f"{label}.type", bool(edge_type), "edge type must be non-empty")
        v.add(f"{label}.from", bool(from_id), "edge from must be non-empty")
        v.add(f"{label}.to", bool(to_id), "edge to must be non-empty")

        if edge_type == "summary_to_claim":
            summary_to_claim_edges.add((from_id, to_id))
            v.add(
                f"{label}.refs",
                from_id in summary_ids and to_id in claim_node_ids,
                "summary_to_claim edge must reference known summary and claim nodes",
            )
        elif edge_type == "claim_to_citation":
            claim_to_citation_edges.add((from_id, to_id))
            v.add(
                f"{label}.refs",
                from_id in claim_node_ids and to_id in citation_node_ids,
                "claim_to_citation edge must reference known claim and citation nodes",
            )
        else:
            v.add(
                f"{label}.known_type",
                False,
                f"unknown edge type '{edge_type}'",
                severity="warning",
            )

    expected_summary_to_claim_edges = {
        (summary_id, f"claim:{claim_id}") for claim_id, summary_id in claim_to_summary.items() if summary_id
    }
    expected_claim_to_citation_edges = {
        (f"claim:{claim_id}", citation_id)
        for claim_id, citation_ids in claim_to_citations.items()
        if claim_id >= 0
        for citation_id in citation_ids
    }
    v.add(
        "edges.summary_to_claim.complete",
        summary_to_claim_edges == expected_summary_to_claim_edges,
        "summary_to_claim edges must exactly match claim summary assignments",
    )
    v.add(
        "edges.claim_to_citation.complete",
        claim_to_citation_edges == expected_claim_to_citation_edges,
        "claim_to_citation edges must exactly match claim citation lists",
    )

    trace_summary_ids: set[str] = set()
    for idx, row in enumerate(trace_summary_rows):
        label = f"traceability.summary_to_source[{idx}]"
        row_obj = _expect_dict(row, label, v)
        summary_id = to_str(row_obj.get("summary_id")).strip()
        trace_summary_ids.add(summary_id)
        v.add(
            f"{label}.summary_id",
            bool(summary_id) and summary_id in summary_ids,
            "summary_id must reference known summary node",
        )

        row_claim_ids = _expect_list(row_obj.get("claim_ids"), f"{label}.claim_ids", v)
        row_claim_set = {to_int(item, default=-1) for item in row_claim_ids if to_int(item, default=-1) >= 0}
        expected_claim_set = summary_to_claim_ids.get(summary_id, set())
        v.add(
            f"{label}.claim_ids.match",
            row_claim_set == expected_claim_set,
            "claim_ids must match summary_graph summary.claim_ids",
        )

        row_source_citations = _expect_list(row_obj.get("source_citations"), f"{label}.source_citations", v)
        row_citation_set: set[str] = set()
        for c_idx, citation in enumerate(row_source_citations):
            c_label = f"{label}.source_citations[{c_idx}]"
            citation_obj = _expect_dict(citation, c_label, v)
            citation_node_id = _citation_node_id_from_any(citation_obj.get("citation_id"))
            v.add(
                f"{c_label}.citation_id",
                bool(citation_node_id),
                "source citation entry must include citation_id",
            )
            if citation_node_id:
                row_citation_set.add(citation_node_id)
        expected_citation_set = summary_to_citation_ids.get(summary_id, set())
        v.add(
            f"{label}.source_citations.match",
            row_citation_set == expected_citation_set,
            "source_citations must match summary_graph summary.citation_ids",
        )

    v.add(
        "traceability.summary_to_source.coverage",
        trace_summary_ids == summary_ids,
        "traceability summary_to_source rows must cover all and only known summaries",
    )

    trace_lineage_claim_ids: set[int] = set()
    for idx, row in enumerate(trace_lineage_rows):
        label = f"traceability.claim_lineage[{idx}]"
        row_obj = _expect_dict(row, label, v)
        claim_id = to_int(row_obj.get("claim_id"), default=-1)
        trace_lineage_claim_ids.add(claim_id)
        v.add(
            f"{label}.claim_id",
            claim_id >= 0 and claim_id in claim_to_summary,
            "claim_id must reference known claim node",
        )

        summary_id = to_str(row_obj.get("summary_id")).strip()
        expected_summary_id = claim_to_summary.get(claim_id, "")
        v.add(
            f"{label}.summary_id.match",
            bool(summary_id) and summary_id == expected_summary_id,
            "summary_id must match claim node summary_id",
        )

        citations = _expect_list(row_obj.get("citations"), f"{label}.citations", v)
        lineage_citation_ids: set[str] = set()
        for c_idx, citation in enumerate(citations):
            c_label = f"{label}.citations[{c_idx}]"
            citation_obj = _expect_dict(citation, c_label, v)
            citation_node_id = _citation_node_id_from_any(citation_obj.get("citation_id"))
            v.add(
                f"{c_label}.citation_id",
                bool(citation_node_id),
                "lineage citation entry must include citation_id",
            )
            if citation_node_id:
                lineage_citation_ids.add(citation_node_id)
        expected_lineage_ids = claim_to_citations.get(claim_id, set())
        v.add(
            f"{label}.citations.match",
            lineage_citation_ids == expected_lineage_ids,
            "lineage citations must match summary_graph claim.citation_ids",
        )

    expected_claim_ids = set(claim_to_summary.keys())
    v.add(
        "traceability.claim_lineage.coverage",
        trace_lineage_claim_ids == expected_claim_ids,
        "traceability claim_lineage rows must cover all and only known claims",
    )

    archived_claims = to_int(run_graph.get("archived_claims"), default=-1)
    candidate_claims = to_int(run_graph.get("candidate_claims"), default=-1)
    v.add(
        "run.archived_matches_nodes",
        archived_claims == len(claim_to_summary),
        "run.archived_claims must match number of claim nodes",
    )
    v.add(
        "run.candidate_not_less_than_archived",
        candidate_claims >= archived_claims,
        "run.candidate_claims must be >= run.archived_claims",
    )

    summary_claim_total = sum(len(claim_ids) for claim_ids in summary_to_claim_ids.values())
    summary_citation_union = set().union(*summary_to_citation_ids.values()) if summary_to_citation_ids else set()
    claim_citation_union = set().union(*claim_to_citations.values()) if claim_to_citations else set()

    v.add(
        "summary.partition_claims",
        summary_claim_total == len(claim_to_summary),
        "summary claim partitions must cover each claim exactly once",
    )
    v.add(
        "citations.coverage.summary_vs_nodes",
        summary_citation_union == citation_node_ids,
        "summary citation sets must match citation nodes",
    )
    v.add(
        "citations.coverage.claims_vs_nodes",
        claim_citation_union == citation_node_ids,
        "claim citation sets must match citation nodes",
    )

    metrics = {
        "summary_nodes": len(summary_ids),
        "claim_nodes": len(claim_to_summary),
        "citation_nodes": len(citation_node_ids),
        "summary_to_claim_edges": len(summary_to_claim_edges),
        "claim_to_citation_edges": len(claim_to_citation_edges),
        "traceability_summary_rows": len(trace_summary_ids),
        "traceability_claim_rows": len(trace_lineage_claim_ids),
        "checks_total": len(v.checks),
        "checks_failed": len(v.errors),
        "checks_warned": len(v.warnings),
    }
    return {
        "passed": v.passed,
        "checks": v.checks,
        "errors": v.errors,
        "warnings": v.warnings,
        "metrics": metrics,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate compaction traceability artifacts and emit a JSON report.")
    parser.add_argument(
        "--artifacts-dir",
        default="artifacts/compaction",
        help="Directory containing summary_graph.json and traceability.json.",
    )
    parser.add_argument(
        "--summary-graph",
        default="",
        help="Optional explicit path to summary_graph.json.",
    )
    parser.add_argument(
        "--traceability",
        default="",
        help="Optional explicit path to traceability.json.",
    )
    parser.add_argument(
        "--out-json",
        default="artifacts/compaction/compaction_trace_validation.json",
        help="Output report JSON path.",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="If artifacts are missing, emit a skipped report and exit 0.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    artifacts_dir = Path(args.artifacts_dir)
    summary_graph_path = Path(args.summary_graph) if to_str(args.summary_graph).strip() else artifacts_dir / "summary_graph.json"
    traceability_path = Path(args.traceability) if to_str(args.traceability).strip() else artifacts_dir / "traceability.json"
    out_json = Path(args.out_json)

    missing = [str(path) for path in (summary_graph_path, traceability_path) if not path.exists()]
    if missing:
        status = "skipped" if args.allow_missing else "fail"
        payload = {
            "timestamp": utc_now(),
            "status": status,
            "passed": bool(args.allow_missing),
            "artifacts": {
                "summary_graph": str(summary_graph_path),
                "traceability": str(traceability_path),
            },
            "checks": [
                {
                    "name": "artifacts.present",
                    "ok": False,
                    "severity": "error" if not args.allow_missing else "warning",
                    "detail": f"missing artifacts: {', '.join(missing)}",
                }
            ],
            "errors": ([] if args.allow_missing else [f"missing artifacts: {', '.join(missing)}"]),
            "warnings": ([f"missing artifacts: {', '.join(missing)}"] if args.allow_missing else []),
            "metrics": {},
        }
        write_json(out_json, payload)
        print(f"status={status} wrote={out_json}")
        return 0 if args.allow_missing else 2

    try:
        summary_graph = load_json_object(summary_graph_path)
        traceability = load_json_object(traceability_path)
    except Exception as exc:
        payload = {
            "timestamp": utc_now(),
            "status": "fail",
            "passed": False,
            "artifacts": {
                "summary_graph": str(summary_graph_path),
                "traceability": str(traceability_path),
            },
            "checks": [],
            "errors": [f"failed to load artifacts: {exc}"],
            "warnings": [],
            "metrics": {},
        }
        write_json(out_json, payload)
        print(f"status=fail wrote={out_json}")
        return 2

    results = validate_payloads(summary_graph, traceability)
    status = "pass" if results.get("passed", False) else "fail"
    payload = {
        "timestamp": utc_now(),
        "status": status,
        "passed": bool(results.get("passed", False)),
        "artifacts": {
            "summary_graph": str(summary_graph_path),
            "traceability": str(traceability_path),
        },
        "checks": results.get("checks", []),
        "errors": results.get("errors", []),
        "warnings": results.get("warnings", []),
        "metrics": results.get("metrics", {}),
    }
    write_json(out_json, payload)
    print(f"status={status} wrote={out_json}")
    return 0 if status == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
