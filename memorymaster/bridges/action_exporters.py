"""Export gateways for approved Atlas action proposals."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memorymaster.bridges.evidence_policy import is_governed_evidence_eligible


@dataclass(frozen=True)
class ActionExportResult:
    destination: str
    output_path: str
    exported: int
    proposal_ids: list[int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "destination": self.destination,
            "output_path": self.output_path,
            "exported": self.exported,
            "proposal_ids": self.proposal_ids,
        }


def export_approved_actions(
    service,
    output_path: str | Path,
    *,
    destination: str = "super-productivity",
    limit: int = 100,
    mark_exported: bool = True,
) -> ActionExportResult:
    proposals = service.list_action_proposals(
        status="approved",
        destination=destination,
        limit=limit,
    )
    needed_evidence_ids = {
        proposal.evidence_item_id
        for proposal in proposals
        if proposal.evidence_item_id is not None
    }
    evidence_by_id = {}
    if needed_evidence_ids:
        evidence_by_id = {
            evidence.id: evidence
            for evidence in service.list_evidence_items(limit=max(needed_evidence_ids))
            if evidence.id in needed_evidence_ids
        }
    proposals = [
        proposal
        for proposal in proposals
        if _proposal_evidence_is_eligible(proposal, evidence_by_id)
    ]
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tasks = [_proposal_to_super_productivity_bridge_task(proposal) for proposal in proposals]
    payload = {
        "format": "atlas-super-productivity-bridge-v1",
        "destination": destination,
        "tasks": tasks,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    exported_ids: list[int] = []
    if mark_exported:
        for proposal in proposals:
            service.update_action_proposal_status(
                proposal.id,
                status="exported",
                external_ref=f"file:{path}#proposal-{proposal.id}",
            )
            exported_ids.append(proposal.id)
    else:
        exported_ids = [proposal.id for proposal in proposals]

    return ActionExportResult(
        destination=destination,
        output_path=str(path),
        exported=len(exported_ids),
        proposal_ids=exported_ids,
    )


def _proposal_evidence_is_eligible(proposal, evidence_by_id: dict[int, Any]) -> bool:
    if proposal.evidence_item_id is None:
        return True
    evidence = evidence_by_id.get(proposal.evidence_item_id)
    return evidence is not None and is_governed_evidence_eligible(evidence)


def _proposal_to_super_productivity_bridge_task(proposal) -> dict[str, Any]:
    notes = proposal.description or ""
    if proposal.source_item_id is not None:
        notes = f"{notes}\n\nAtlas source_item_id: {proposal.source_item_id}".strip()
    if proposal.evidence_item_id is not None:
        notes = f"{notes}\nAtlas evidence_item_id: {proposal.evidence_item_id}".strip()
    return {
        "title": proposal.title,
        "notes": notes,
        "due": proposal.suggested_due_at,
        "atlas_proposal_id": proposal.id,
        "atlas_confidence": proposal.confidence,
        "atlas_payload": _json_or_raw(proposal.payload_json),
    }


def _json_or_raw(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
