"""Bounded governed claim-to-evidence rehydration for offline experiments."""

from __future__ import annotations

import contextlib
from typing import Any, Sequence

from memorymaster.core.security import is_sensitive_claim, scan_text_for_findings


REPORT_SCHEMA = "memorymaster.evidence-rehydration.v1"


def _authorized_claim(service: Any, claim_id: int, scopes: set[str]) -> Any | None:
    claim = service.store.get_claim(claim_id)
    if claim is None or claim.status != "confirmed" or claim.scope not in scopes:
        return None
    if getattr(claim, "visibility", "public") != "public" or is_sensitive_claim(claim):
        return None
    return claim


def _evidence_rows(service: Any, claim_id: int, limit: int) -> tuple[list[dict[str, Any]], bool]:
    with contextlib.closing(service.store.connect()) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM claim_evidence_links WHERE claim_id=?", (claim_id,)
        ).fetchone()[0]
        rows = conn.execute(
            """SELECT e.id, e.text FROM claim_evidence_links l
               JOIN evidence_items e ON e.id=l.evidence_item_id
               JOIN source_items s ON s.id=e.source_item_id
               WHERE l.claim_id=? AND s.retired_at IS NULL AND e.text IS NOT NULL
                 AND COALESCE(e.sensitivity, 'none') NOT IN ('high','redacted')
                 AND COALESCE(s.sensitivity, 'none') NOT IN ('high','redacted')
               ORDER BY e.id LIMIT ?""",
            (claim_id, limit),
        ).fetchall()
    evidence = [
        {"evidence_id": int(row[0]), "excerpt": str(row[1])}
        for row in rows
        if not scan_text_for_findings(str(row[1]))
    ]
    return evidence, bool(total)


def _ordered_ids(
    seed_ids: Sequence[int], graph_ids: Sequence[int], *, max_claims: int, max_graph_claims: int
) -> tuple[list[int], set[int]]:
    seeds = list(dict.fromkeys(int(value) for value in seed_ids))[:max_claims]
    graph = [value for value in dict.fromkeys(int(value) for value in graph_ids) if value not in seeds]
    remaining = max(0, max_claims - len(seeds))
    selected_graph = graph[: min(max_graph_claims, remaining)]
    return [*seeds, *selected_graph], set(selected_graph)


def rehydrate_claim_evidence(
    service: Any,
    claim_ids: Sequence[int],
    *,
    scope_allowlist: Sequence[str],
    graph_signal_claim_ids: Sequence[int] = (),
    max_claims: int = 10,
    max_graph_claims: int = 3,
    max_evidence_per_claim: int = 5,
) -> dict[str, Any]:
    if not 1 <= max_claims <= 50 or not 0 <= max_graph_claims <= 10:
        raise ValueError("claim bounds are outside the supported range")
    if not 1 <= max_evidence_per_claim <= 20:
        raise ValueError("evidence bound is outside the supported range")
    scopes = {scope for scope in scope_allowlist if scope}
    ordered, graph_selected = _ordered_ids(
        claim_ids, graph_signal_claim_ids, max_claims=max_claims, max_graph_claims=max_graph_claims
    )
    claims: list[dict[str, Any]] = []
    accepted_graph: list[int] = []
    for claim_id in ordered:
        claim = _authorized_claim(service, claim_id, scopes)
        if claim is None:
            continue
        evidence, had_links = _evidence_rows(service, claim_id, max_evidence_per_claim)
        if had_links and not evidence:
            continue
        claims.append({"claim_id": claim_id, "evidence": evidence})
        if claim_id in graph_selected:
            accepted_graph.append(claim_id)
    if not claims:
        fallback = "no_authorized_evidence"
    elif any(not row["evidence"] for row in claims):
        fallback = "insufficient_evidence"
    else:
        fallback = "none"
    return {
        "schema_version": REPORT_SCHEMA,
        "claims": claims,
        "graph_signal_ids": accepted_graph,
        "fallback_reason": fallback,
    }


__all__ = ["REPORT_SCHEMA", "rehydrate_claim_evidence"]
