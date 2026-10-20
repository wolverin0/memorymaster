"""Creation attribution shared by atomic stores and the ingest facade."""

from __future__ import annotations

from typing import Any

from memorymaster.core.models import Claim
from memorymaster.core.security import validate_persisted_metadata


def prepare_outcome_receipt(receipt: dict[str, Any] | None) -> str | None:
    if receipt is None:
        return None
    if not isinstance(receipt, dict):
        raise ValueError("outcome_receipt must be a dictionary")
    operation = receipt.get("operation_id")
    if operation is not None and (not isinstance(operation, str) or not operation.strip() or len(operation) > 200):
        raise ValueError("ingest operation_id must be bounded nonempty text")
    validate_persisted_metadata({"ingest_operation_id": operation})
    receipt.clear()
    receipt["operation_id"] = operation
    return operation


def creation_payload(citation_count: int, operation_id: str | None, intake_batch_id: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"citation_count": citation_count}
    if operation_id is not None:
        payload["ingest_operation_id"] = operation_id
    if intake_batch_id is not None:
        validate_persisted_metadata({"intake_batch_id": intake_batch_id})
        payload["intake_batch_id"] = intake_batch_id
    return payload


def fill_outcome_receipt(
    receipt: dict[str, Any] | None, claim: Claim, *, created: bool,
    persisted: dict[str, Any] | None = None,
) -> None:
    if receipt is None:
        return
    persisted = persisted or {}
    operation = receipt.get("operation_id")
    creation_operation = persisted.get("ingest_operation_id")
    blocked = claim.status == "archived"
    receipt.update({
        "status": "blocked" if blocked else "created" if created else "reused",
        "claim_id": claim.id, "created": bool(created),
        "owned": bool(operation and creation_operation == operation),
        "creation_operation_id": creation_operation,
        "intake_batch_id": persisted.get("intake_batch_id"),
        "citation_count": persisted.get("citation_count"),
    })
