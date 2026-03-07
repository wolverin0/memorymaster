from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return payload


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _render_markdown(
    *,
    summary_graph: dict[str, Any],
    traceability: dict[str, Any],
    summary_graph_path: Path,
    traceability_path: Path,
) -> str:
    run = summary_graph.get("run", {})
    nodes = summary_graph.get("nodes", {})
    summary_nodes = nodes.get("summaries", [])
    claim_nodes = nodes.get("claims", [])
    citation_nodes = nodes.get("citations", [])
    edges = summary_graph.get("edges", [])
    summary_to_source = traceability.get("summary_to_source", [])
    claim_lineage = traceability.get("claim_lineage", [])

    lines: list[str] = []
    lines.append("# Compaction Traceability Report")
    lines.append("")
    lines.append("## Artifacts")
    lines.append(f"- summary_graph: `{summary_graph_path}`")
    lines.append(f"- traceability: `{traceability_path}`")
    lines.append("")
    lines.append("## Run Summary")
    lines.append(f"- run_id: `{run.get('run_id', '-')}`")
    lines.append(f"- generated_at: `{run.get('generated_at', '-')}`")
    lines.append(f"- retain_days: `{run.get('retain_days', '-')}`")
    lines.append(f"- event_retain_days: `{run.get('event_retain_days', '-')}`")
    lines.append(f"- candidate_claims: `{run.get('candidate_claims', 0)}`")
    lines.append(f"- archived_claims: `{run.get('archived_claims', 0)}`")
    lines.append(f"- deleted_events: `{run.get('deleted_events', 0)}`")
    lines.append("")
    lines.append("## Graph Snapshot")
    lines.append(f"- summary_nodes: `{len(summary_nodes)}`")
    lines.append(f"- claim_nodes: `{len(claim_nodes)}`")
    lines.append(f"- citation_nodes: `{len(citation_nodes)}`")
    lines.append(f"- edges: `{len(edges)}`")
    lines.append("")
    lines.append("## Summary Groups")
    if not summary_nodes:
        lines.append("- No summary groups were emitted.")
    else:
        for node in summary_nodes:
            summary_id = str(node.get("id", "-"))
            subject = node.get("subject") or "-"
            predicate = node.get("predicate") or "-"
            scope = node.get("scope") or "-"
            claim_count = _safe_int(node.get("claim_count"))
            citation_count = _safe_int(node.get("citation_count"))
            lines.append(
                f"- `{summary_id}`: subject=`{subject}` predicate=`{predicate}` scope=`{scope}` "
                f"claims=`{claim_count}` citations=`{citation_count}`"
            )
    lines.append("")
    lines.append("## Claim Lineage")
    if not claim_lineage:
        lines.append("- No compacted claims in this run.")
    else:
        for row in claim_lineage:
            claim_id = row.get("claim_id")
            summary_id = row.get("summary_id", "-")
            status_before = row.get("status_before", "-")
            status_after = row.get("status_after", "-")
            citations = row.get("citations", [])
            lines.append(
                f"- claim `{claim_id}`: `{status_before}` -> `{status_after}` "
                f"(summary `{summary_id}`, citations `{len(citations)}`)"
            )
            for citation in citations:
                source = citation.get("source", "-")
                locator = citation.get("locator") or "-"
                excerpt = citation.get("excerpt") or "-"
                lines.append(f"  - source=`{source}` locator=`{locator}` excerpt=`{excerpt}`")
    lines.append("")
    lines.append("## Summary to Source Links")
    if not summary_to_source:
        lines.append("- No summary-to-source mappings.")
    else:
        for row in summary_to_source:
            summary_id = row.get("summary_id", "-")
            claim_ids = row.get("claim_ids", [])
            source_citations = row.get("source_citations", [])
            lines.append(
                f"- `{summary_id}`: claims={len(claim_ids)} source_citations={len(source_citations)}"
            )
    lines.append("")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render markdown report from compaction trace artifacts.")
    parser.add_argument(
        "--artifacts-dir",
        default="artifacts/compaction",
        help="Directory containing summary_graph.json and traceability.json.",
    )
    parser.add_argument(
        "--summary-graph",
        default="",
        help="Optional explicit path to summary_graph.json (defaults to --artifacts-dir/summary_graph.json).",
    )
    parser.add_argument(
        "--traceability",
        default="",
        help="Optional explicit path to traceability.json (defaults to --artifacts-dir/traceability.json).",
    )
    parser.add_argument(
        "--out-md",
        default="artifacts/compaction/compaction_trace_report.md",
        help="Output markdown path.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    artifacts_dir = Path(args.artifacts_dir)
    summary_graph_path = Path(args.summary_graph) if str(args.summary_graph).strip() else artifacts_dir / "summary_graph.json"
    traceability_path = Path(args.traceability) if str(args.traceability).strip() else artifacts_dir / "traceability.json"
    out_path = Path(args.out_md)

    if not summary_graph_path.exists():
        print(f"error: missing summary graph artifact: {summary_graph_path}")
        return 2
    if not traceability_path.exists():
        print(f"error: missing traceability artifact: {traceability_path}")
        return 2

    summary_graph = _load_json(summary_graph_path)
    traceability = _load_json(traceability_path)

    markdown = _render_markdown(
        summary_graph=summary_graph,
        traceability=traceability,
        summary_graph_path=summary_graph_path,
        traceability_path=traceability_path,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    print(f"wrote={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
