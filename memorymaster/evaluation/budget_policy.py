"""Deterministic shadow budget and admission policies for paper experiments."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Sequence

from memorymaster.evaluation.sustainability import StageObservation


POLICY_SCHEMA = "memorymaster.shadow-budget-policy.v1"
REPORT_SCHEMA = "memorymaster.shadow-admission-report.v1"
_TOKENS = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    name: str
    retrieval_mode: str
    token_budget: int
    candidate_limit: int
    graph_expansion: bool
    evidence_sufficiency: bool
    include_skills: bool
    provider_calls_allowed: int


POLICIES = {
    "low": BudgetPolicy("low", "legacy", 1000, 8, False, False, False, 0),
    "balanced": BudgetPolicy("balanced", "hybrid", 4000, 20, True, False, False, 1),
    "high": BudgetPolicy("high", "hybrid", 8000, 50, True, True, False, 1),
    "temporal": BudgetPolicy("temporal", "hybrid", 6000, 40, True, True, False, 1),
    "procedural": BudgetPolicy("procedural", "hybrid", 5000, 30, True, True, True, 1),
}


def get_policy(requested_tier: str) -> BudgetPolicy:
    try:
        return POLICIES[requested_tier]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"requested_tier must be one of {tuple(POLICIES)}") from exc


def _authorized(rows: Iterable[dict[str, Any]], scopes: set[str]) -> list[dict[str, Any]]:
    scoped = [row for row in rows if str(row.get("scope", "")) in scopes]
    public = [row for row in scoped if row.get("sensitive") is False]
    return [row for row in public if row.get("status") == "confirmed"]


def _normalized(text: Any) -> str:
    return " ".join(_TOKENS.findall(str(text or "").casefold()))


def _token_set(text: Any) -> set[str]:
    return set(_TOKENS.findall(str(text or "").casefold()))


def _near_duplicate(text: Any, accepted: Sequence[dict[str, Any]]) -> bool:
    tokens = _token_set(text)
    if not tokens:
        return False
    for row in accepted:
        other = _token_set(row.get("text"))
        union = tokens | other
        if union and len(tokens & other) / len(union) >= 0.8:
            return True
    return False


def _admit_rows(
    rows: Sequence[dict[str, Any]], policy: BudgetPolicy
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    admitted: list[dict[str, Any]] = []
    diagnostics: dict[str, list[str]] = {}
    seen: set[str] = set()
    for row in rows:
        row_id = str(row.get("id", ""))
        normalized = _normalized(row.get("text"))
        reasons: list[str] = []
        if normalized in seen:
            reasons.append("redundant")
        elif _near_duplicate(row.get("text"), admitted):
            reasons.append("near_duplicate")
        if int(row.get("evidence_count", 0) or 0) <= 0 or float(row.get("confidence", 0) or 0) < 0.6:
            reasons.append("weak_support")
        if reasons:
            diagnostics[row_id] = reasons
            continue
        if len(admitted) >= policy.candidate_limit:
            diagnostics[row_id] = ["budget_limit"]
            continue
        admitted.append(row)
        seen.add(normalized)
    return admitted, diagnostics


def _mark_conflicts(
    admitted: Sequence[dict[str, Any]], diagnostics: dict[str, list[str]]
) -> None:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in admitted:
        key = (str(row.get("subject", "")), str(row.get("predicate", "")))
        if all(key):
            groups.setdefault(key, []).append(row)
    for rows in groups.values():
        objects = {str(row.get("object_value", "")) for row in rows}
        if len(objects) <= 1:
            continue
        for row in rows:
            diagnostics.setdefault(str(row["id"]), []).append("lifecycle_conflict")


def shadow_admit(
    rows: Sequence[dict[str, Any]],
    *,
    requested_tier: str,
    scope_allowlist: Sequence[str],
) -> dict[str, Any]:
    scopes = {scope for scope in scope_allowlist if isinstance(scope, str) and scope}
    authorized = _authorized(rows, scopes)
    policy = get_policy(requested_tier)
    admitted, diagnostics = _admit_rows(authorized, policy)
    _mark_conflicts(admitted, diagnostics)
    return {
        "schema_version": REPORT_SCHEMA,
        "policy_schema_version": POLICY_SCHEMA,
        "pipeline": ["scope_filter", "sensitivity_filter", "policy_selection", "admission"],
        "requested_count": len(rows),
        "authorized_count": len(authorized),
        "admitted_ids": [str(row["id"]) for row in admitted],
        "diagnostics": dict(sorted(diagnostics.items())),
        "policy": asdict(policy),
        "provider_calls": 0,
    }


def admission_observation(report: dict[str, Any], *, elapsed_ms: float) -> StageObservation:
    policy = report.get("policy", {})
    return StageObservation(
        stage="admission",
        elapsed_ms=elapsed_ms,
        selected_tier=str(policy.get("name", "")),
        provider_calls=int(report.get("provider_calls", 0)),
    )


__all__ = [
    "POLICIES",
    "POLICY_SCHEMA",
    "REPORT_SCHEMA",
    "BudgetPolicy",
    "admission_observation",
    "get_policy",
    "shadow_admit",
]
