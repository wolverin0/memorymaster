"""Confirmed-claim graph extraction at the governed capture worker boundary.

This module resolves a replay-safe graph job back to its confirmed claim,
invokes the configured ontology-aware EntityGraph, and converts diagnostics
into durable blocked-job codes. Read this when changing graph job identity,
eligibility, or provider failure behavior.
"""

from __future__ import annotations

import hashlib
from typing import Any

from memorymaster.capture.adapters import CaptureRejected
from memorymaster.knowledge.entity_graph import EntityGraph


def _claim_job_hash(claim: Any) -> str:
    value = f"claim:{claim.id}:{claim.updated_at}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _matching_claim(repository: Any, job: Any) -> Any | None:
    for claim in repository.claims_for_source(job.source_item_id):
        if claim.status == "confirmed" and _claim_job_hash(claim) == job.content_hash:
            return claim
    return None


def _diagnostic_error(diagnostics: list[str]) -> CaptureRejected:
    detail = ", ".join(diagnostics)
    ontology_codes = ("unknown_", "malformed_")
    if any(item.startswith(ontology_codes) for item in diagnostics):
        return CaptureRejected("ontology_validation_failed", detail)
    return CaptureRejected("graph_claim_ineligible", detail)


def extract_confirmed_claim_graph(
    service: Any, repository: Any, job: Any
) -> list[str]:
    """Extract one eligible confirmed claim or fail into actionable diagnostics."""
    if getattr(repository, "postgres", False):
        raise CaptureRejected(
            "graph_backend_unavailable",
            "Graph extraction runtime currently requires the local SQLite store.",
        )
    source = service.get_source_item_by_id(job.source_item_id)
    if source is None or source.retired_at is not None:
        raise CaptureRejected("source_unavailable", "Source is missing or retired.")
    claim = _matching_claim(repository, job)
    if claim is None:
        raise CaptureRejected(
            "graph_claim_unavailable",
            "No active confirmed claim matches this graph job identity.",
        )
    graph = EntityGraph(str(service.store.db_path))
    entities = graph.extract_and_link(claim.id, claim.text)
    if graph.last_diagnostics:
        raise _diagnostic_error(graph.last_diagnostics)
    return entities
