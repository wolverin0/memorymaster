"""Read-time revalidation and bounded packing for opt-in observations."""

from __future__ import annotations

import re
from typing import Any

from memorymaster.knowledge.graph_observation_repository import (
    GraphObservationRepository,
)
from memorymaster.recall.context_optimizer import estimate_tokens


OBSERVATION_HEADER = "=== DERIVED OBSERVATIONS ===\nEvidence-bound patterns; lifecycle labels are authoritative."
_WORD = re.compile(r"[a-z0-9_]{2,}")


def _score(query: str, row: dict[str, Any]) -> tuple[int, int]:
    terms = set(_WORD.findall(query.lower()))
    text = f"{row['name']} {row['text']} {row['observation_type']}".lower()
    claim_id = row.get("observation_claim_id", row.get("claim_id", 0))
    return sum(term in text for term in terms), int(claim_id)


def _public_row(
    repo: GraphObservationRepository,
    row: dict[str, Any],
    *,
    support_valid: bool,
) -> dict[str, Any]:
    supports = repo.observation_support_rows(int(row["observation_claim_id"]))
    relationships = sorted(
        {
            (
                int(item["source_entity_id"]),
                str(item["relation"]),
                int(item["target_entity_id"]),
                str(item["ontology_version"]),
            )
            for item in supports
        }
    )
    return {
        "claim_id": int(row["observation_claim_id"]),
        "name": str(row["name"]),
        "observation_type": str(row["observation_type"]),
        "summary": str(row["text"]),
        "status": str(row["status"]),
        "scope": str(row["scope"]),
        "confidence": float(row["confidence"]),
        "support_hash": str(row["support_hash"]),
        "support_valid": support_valid,
        "evidence_window_start": row["evidence_window_start"],
        "evidence_window_end": row["evidence_window_end"],
        "supporting_claim_ids": sorted({int(item["supporting_claim_id"]) for item in supports}),
        "evidence_item_ids": sorted({int(item["evidence_item_id"]) for item in supports}),
        "source_item_ids": sorted({int(item["source_item_id"]) for item in supports}),
        "relationships": relationships,
    }


def recall_observations(
    service: Any,
    query: str,
    *,
    scopes: list[str],
    trust_mode: str,
    limit: int,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    repo = GraphObservationRepository(service.store)
    candidates: list[dict[str, Any]] = []
    for scope in scopes:
        for row in repo.scope_observations(scope=scope, tenant_id=tenant_id):
            status = str(row["status"])
            valid, _reason = repo.observation_gate(int(row["observation_claim_id"]))
            if trust_mode == "trusted" and (status != "confirmed" or not valid):
                continue
            if trust_mode != "trusted" and status not in {"candidate", "confirmed", "stale"}:
                continue
            candidates.append(_public_row(repo, row, support_valid=valid))
    candidates.sort(key=lambda row: _score(query, {**row, "text": row["summary"]}), reverse=True)
    return candidates[: max(1, min(int(limit), 5))]


def _block(observation: dict[str, Any]) -> str:
    window = " -> ".join(
        str(value or "unknown")
        for value in (
            observation["evidence_window_start"],
            observation["evidence_window_end"],
        )
    )
    relationships = ", ".join(
        f"{source}:{relation}:{target}@{version}"
        for source, relation, target, version in observation["relationships"]
    )
    return "\n".join(
        [
            (
                f"[observation claim_id={observation['claim_id']} "
                f"status={observation['status']} type={observation['observation_type']}]"
            ),
            f"Name: {observation['name']}",
            f"Summary: {observation['summary']}",
            f"Evidence window: {window}",
            f"Supporting claims: {observation['supporting_claim_ids']}",
            f"Evidence items: {observation['evidence_item_ids']}",
            f"Relationships: {relationships}",
        ]
    )


def pack_observations(
    observations: list[dict[str, Any]], *, token_budget: int
) -> tuple[str, tuple[dict[str, Any], ...]]:
    if token_budget <= estimate_tokens(OBSERVATION_HEADER):
        return "", ()
    selected: list[dict[str, Any]] = []
    blocks: list[str] = []
    for observation in observations:
        block = _block(observation)
        candidate = f"{OBSERVATION_HEADER}\n\n" + "\n\n".join((*blocks, block))
        if estimate_tokens(candidate) > token_budget:
            continue
        selected.append(observation)
        blocks.append(block)
    if not blocks:
        return "", ()
    return f"{OBSERVATION_HEADER}\n\n" + "\n\n".join(blocks), tuple(selected)
