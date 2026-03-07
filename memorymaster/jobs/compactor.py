from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memorymaster.lifecycle import transition_claim


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _summary_node_id(subject: str | None, predicate: str | None, scope: str | None) -> str:
    raw = "|".join([(subject or "").strip(), (predicate or "").strip(), (scope or "project").strip()])
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"summary:{digest}"


def run(
    store,
    retain_days: int = 30,
    event_retain_days: int = 60,
    artifacts_dir: str | Path | None = None,
) -> dict[str, int]:
    generated_at = _utc_now_iso()
    run_id = f"compaction-{generated_at}"
    out_dir = Path(artifacts_dir) if artifacts_dir is not None else (Path("artifacts") / "compaction")
    summary_graph_path = out_dir / "summary_graph.json"
    traceability_path = out_dir / "traceability.json"

    archive_candidates = store.find_for_compaction(retain_days=retain_days)
    archived = 0
    claim_nodes: list[dict[str, Any]] = []
    citation_nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, str]] = []
    summary_claim_ids: dict[str, list[int]] = {}
    summary_citation_ids: dict[str, set[str]] = {}
    summary_status_counts: dict[str, dict[str, int]] = {}
    summary_meta: dict[str, dict[str, str | None]] = {}
    claim_lineage_rows: list[dict[str, Any]] = []

    for claim in archive_candidates:
        summary_id = _summary_node_id(claim.subject, claim.predicate, claim.scope)
        summary_claim_ids.setdefault(summary_id, []).append(claim.id)
        summary_citation_ids.setdefault(summary_id, set())
        summary_status_counts.setdefault(summary_id, {})
        summary_meta.setdefault(
            summary_id,
            {
                "subject": claim.subject,
                "predicate": claim.predicate,
                "scope": claim.scope,
            },
        )
        summary_status_counts[summary_id][claim.status] = summary_status_counts[summary_id].get(claim.status, 0) + 1
        edges.append({"type": "summary_to_claim", "from": summary_id, "to": f"claim:{claim.id}"})

        claim_citations = store.list_citations(claim.id)
        claim_citation_ids: list[str] = []
        citation_refs: list[dict[str, Any]] = []
        for citation in claim_citations:
            citation_id = f"citation:{citation.id}"
            claim_citation_ids.append(citation_id)
            summary_citation_ids[summary_id].add(citation_id)
            if citation_id not in citation_nodes:
                citation_nodes[citation_id] = {
                    "id": citation_id,
                    "type": "citation",
                    "citation_id": citation.id,
                    "claim_id": citation.claim_id,
                    "source": citation.source,
                    "locator": citation.locator,
                    "excerpt": citation.excerpt,
                    "created_at": citation.created_at,
                }
            citation_refs.append(
                {
                    "citation_id": citation.id,
                    "source": citation.source,
                    "locator": citation.locator,
                    "excerpt": citation.excerpt,
                    "created_at": citation.created_at,
                }
            )
            edges.append({"type": "claim_to_citation", "from": f"claim:{claim.id}", "to": citation_id})

        transition_claim(
            store,
            claim_id=claim.id,
            to_status="archived",
            reason=f"compacted after inactivity ({retain_days}d) in {claim.status}",
            event_type="compactor",
        )
        archived += 1
        claim_nodes.append(
            {
                "id": f"claim:{claim.id}",
                "type": "claim",
                "claim_id": claim.id,
                "summary_id": summary_id,
                "status_before": claim.status,
                "status_after": "archived",
                "text": claim.text,
                "claim_type": claim.claim_type,
                "subject": claim.subject,
                "predicate": claim.predicate,
                "object_value": claim.object_value,
                "scope": claim.scope,
                "confidence": claim.confidence,
                "updated_at_before": claim.updated_at,
                "citation_ids": claim_citation_ids,
            }
        )
        claim_lineage_rows.append(
            {
                "claim_id": claim.id,
                "summary_id": summary_id,
                "status_before": claim.status,
                "status_after": "archived",
                "claim_text": claim.text,
                "citations": citation_refs,
            }
        )

    deleted_events = store.delete_old_events(event_retain_days)
    summary_nodes: list[dict[str, Any]] = []
    summary_to_source: list[dict[str, Any]] = []
    for summary_id, claim_ids in summary_claim_ids.items():
        citation_ids = sorted(summary_citation_ids.get(summary_id, set()))
        summary_nodes.append(
            {
                "id": summary_id,
                "type": "summary",
                "subject": summary_meta[summary_id]["subject"],
                "predicate": summary_meta[summary_id]["predicate"],
                "scope": summary_meta[summary_id]["scope"],
                "claim_count": len(claim_ids),
                "citation_count": len(citation_ids),
                "status_before_counts": summary_status_counts.get(summary_id, {}),
                "claim_ids": sorted(claim_ids),
                "citation_ids": citation_ids,
            }
        )
        summary_to_source.append(
            {
                "summary_id": summary_id,
                "claim_ids": sorted(claim_ids),
                "source_citations": [citation_nodes[citation_id] for citation_id in citation_ids],
            }
        )

    run_metadata: dict[str, Any] = {
        "run_id": run_id,
        "generated_at": generated_at,
        "retain_days": retain_days,
        "event_retain_days": event_retain_days,
        "candidate_claims": len(archive_candidates),
        "archived_claims": archived,
        "deleted_events": deleted_events,
    }

    summary_graph = {
        "schema_version": "1.0",
        "artifact_type": "summary_graph",
        "run": run_metadata,
        "nodes": {
            "summaries": sorted(summary_nodes, key=lambda row: str(row["id"])),
            "claims": sorted(claim_nodes, key=lambda row: int(row["claim_id"])),
            "citations": sorted(citation_nodes.values(), key=lambda row: int(row["citation_id"])),
        },
        "edges": edges,
    }
    traceability = {
        "schema_version": "1.0",
        "artifact_type": "traceability",
        "run": run_metadata,
        "summary_to_source": sorted(summary_to_source, key=lambda row: str(row["summary_id"])),
        "claim_lineage": sorted(claim_lineage_rows, key=lambda row: int(row["claim_id"])),
    }

    _write_json(summary_graph_path, summary_graph)
    _write_json(traceability_path, traceability)
    store.record_event(
        claim_id=None,
        event_type="compaction_run",
        details="compaction_completed",
        payload={
            "retain_days": retain_days,
            "event_retain_days": event_retain_days,
            "archived_claims": archived,
            "deleted_events": deleted_events,
            "artifacts": {
                "summary_graph": str(summary_graph_path),
                "traceability": str(traceability_path),
            },
        },
    )
    return {"archived_claims": archived, "deleted_events": deleted_events}
