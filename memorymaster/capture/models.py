"""Immutable data contracts for governed capture persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CaptureStage = Literal["extract_text", "extract_claims", "extract_graph"]
CaptureJobStatus = Literal[
    "pending", "leased", "retryable", "blocked", "completed", "cancelled"
]

CAPTURE_STAGES = frozenset({"extract_text", "extract_claims", "extract_graph"})
CAPTURE_JOB_STATUSES = frozenset(
    {"pending", "leased", "retryable", "blocked", "completed", "cancelled"}
)


@dataclass(frozen=True, slots=True)
class ClaimEvidenceLink:
    claim_id: int
    evidence_item_id: int
    role: str
    created_at: str


@dataclass(frozen=True, slots=True)
class CaptureJob:
    id: int
    source_item_id: int
    content_hash: str
    stage: CaptureStage
    status: CaptureJobStatus
    attempts: int
    next_attempt_at: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    error_code: str | None
    error_detail: str | None
    created_at: str
    updated_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class EdgeSupport:
    source_entity_id: int
    target_entity_id: int
    relation: str
    supporting_claim_id: int
    scope: str
    ontology_version: str
    created_at: str
