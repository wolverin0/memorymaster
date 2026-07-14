"""Mutation application for the dashboard composition root."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable


def update_action_proposal_status(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    proposal_id = int(payload.get("proposal_id") or 0)
    status = str(payload.get("status") or "").strip().lower()
    external_ref = payload.get("external_ref")
    if proposal_id <= 0:
        raise ValueError("proposal_id must be positive")
    proposal = service.update_action_proposal_status(
        proposal_id,
        status=status,
        external_ref=str(external_ref) if external_ref not in (None, "") else None,
    )
    return {"ok": True, "proposal": asdict(proposal)}


def apply_triage_action(
    service: Any,
    payload: dict[str, Any],
    *,
    serialize_claim: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    action = str(payload.get("action") or "").strip().lower()
    claim_id = int(payload.get("claim_id", 0))
    allowed = {"pin", "unpin", "mark_reviewed", "suppress", "unsuppress", "approve_proposal", "reject_proposal"}
    if claim_id <= 0:
        raise ValueError("claim_id must be positive")
    if action not in allowed:
        raise ValueError("unsupported action")
    if action in {"pin", "unpin"}:
        claim = service.pin(claim_id, pin=action == "pin")
        return {"ok": True, "action": action, "claim": serialize_claim(claim)}
    if action in {"approve_proposal", "reject_proposal"}:
        proposal_event_id = payload.get("proposal_event_id")
        if type(proposal_event_id) is not int or proposal_event_id <= 0:
            raise ValueError("proposal_event_id must be positive for proposal actions")
        return _resolve_proposal(service, action, proposal_event_id)
    details = {"mark_reviewed": "triage_mark_reviewed", "suppress": "triage_suppress", "unsuppress": "triage_unsuppress"}
    service.store.record_event(
        claim_id=claim_id, event_type="audit", details=details[action], payload={"source": "dashboard"}
    )
    return {"ok": True, "action": action, "claim_id": claim_id}


def _resolve_proposal(service: Any, action: str, proposal_event_id: int) -> dict[str, Any]:
    from memorymaster.govern.steward import resolve_steward_proposal

    result = resolve_steward_proposal(
        service,
        action="approve" if action == "approve_proposal" else "reject",
        proposal_event_id=proposal_event_id,
        apply_on_approve=True,
    )
    return {"ok": True, "action": action, "result": result}


def control_operator(server: Any, payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "").strip().lower()
    if action == "start":
        path = str(payload.get("inbox_jsonl") or "artifacts/operator/operator_inbox.jsonl")
        return {"ok": True, **server.start_operator(path)}
    if action == "stop":
        return {"ok": True, **server.stop_operator()}
    raise ValueError("action must be start or stop")
