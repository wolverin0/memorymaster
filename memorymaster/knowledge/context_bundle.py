"""Build governed recall bundles with optional confirmed personal skills.

Use this module when an agent surface needs ordinary claim context plus
operator-approved ``personal-skill-v1`` workflows. Candidate or out-of-scope
skills never enter the bundle, and the combined output stays within one token
budget. Ordinary recall remains unchanged unless skill inclusion is requested.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from memorymaster.knowledge.skill_schema import is_skill
from memorymaster.knowledge.skills import recall_skills
from memorymaster.knowledge.graph_observation_recall import (
    pack_observations,
    recall_observations,
)
from memorymaster.knowledge.graph_observation_repository import (
    GraphObservationRepository,
)
from memorymaster.recall.context_optimizer import estimate_tokens, pack_context


_SKILL_HEADER = (
    "=== APPROVED SKILLS ===\n"
    "The following workflows are operator-confirmed and authorized for this scope."
)


@dataclass(frozen=True, slots=True)
class ContextBundle:
    """Combined governed claim context and approved skill assets."""

    output: str
    rows: tuple[dict[str, Any], ...]
    skills: tuple[dict[str, Any], ...]
    tokens_used: int
    token_budget: int
    output_format: str
    observations: tuple[dict[str, Any], ...] = ()


def _lines(label: str, values: list[str], *, numbered: bool = False) -> list[str]:
    if not values:
        return []
    prefix = (lambda index: f"{index}.") if numbered else (lambda _index: "-")
    return [f"{label}:", *(f"  {prefix(index)} {value}" for index, value in enumerate(values, 1))]


def _skill_block(skill: dict[str, Any]) -> str:
    lines = [
        (
            f"[skill claim_id={skill['claim_id']} slug={skill['slug']} "
            f"version={skill['skill_version']} scope={skill['scope']}]"
        ),
        f"Title: {skill['title']}",
        f"Use when: {skill['when_to_use']}",
        f"Do not use when: {skill['when_not_to_use']}",
    ]
    lines.extend(_lines("Workflow", skill["workflow"], numbered=True))
    lines.extend(_lines("Decision rules", skill["decision_rules"]))
    lines.append(f"Expected output: {skill['expected_output']}")
    lines.extend(_lines("Validation", skill["validation"]))
    for citation in skill.get("citations", []):
        locator = citation.get("locator") or ""
        lines.append(f"Citation: {citation.get('source', '')} | {locator}".rstrip())
    return "\n".join(lines)


def pack_approved_skills(
    skills: list[dict[str, Any]], *, token_budget: int
) -> tuple[str, tuple[dict[str, Any], ...]]:
    """Pack whole approved skills without truncating executable workflows."""
    if token_budget <= estimate_tokens(_SKILL_HEADER):
        return "", ()
    selected: list[dict[str, Any]] = []
    blocks: list[str] = []
    for skill in skills:
        block = _skill_block(skill)
        candidate = f"{_SKILL_HEADER}\n\n" + "\n\n".join((*blocks, block))
        if estimate_tokens(candidate) > token_budget:
            continue
        selected.append(skill)
        blocks.append(block)
    if not blocks:
        return "", ()
    return f"{_SKILL_HEADER}\n\n" + "\n\n".join(blocks), tuple(selected)


def query_context_bundle(
    service: Any,
    query: str,
    *,
    scope_allowlist: list[str],
    token_budget: int = 4000,
    trust_mode: str = "trusted",
    output_format: str = "text",
    retrieval_mode: str = "hybrid",
    include_skills: bool = False,
    skill_limit: int = 3,
    include_observations: bool = False,
    observation_limit: int = 2,
    observation_tenant_id: str | None = None,
) -> ContextBundle:
    """Query governed claims and optionally append confirmed scoped skills."""
    if token_budget <= 0:
        raise ValueError("token_budget must be positive.")
    if (include_skills or include_observations) and output_format != "text":
        raise ValueError("Derived recall sections require text output format.")
    observation_text = ""
    observations: tuple[dict[str, Any], ...] = ()
    if include_observations:
        observation_budget = min(800, max(1, token_budget // 4))
        candidates = recall_observations(
            service,
            query,
            scopes=scope_allowlist,
            trust_mode=trust_mode,
            limit=max(1, min(int(observation_limit), 5)),
            tenant_id=observation_tenant_id,
        )
        observation_text, observations = pack_observations(
            candidates, token_budget=observation_budget
        )
    observation_reserved = estimate_tokens(observation_text) + 1 if observation_text else 0
    skill_text, skills = _selected_skill_text(
        service,
        query,
        scopes=scope_allowlist,
        total_budget=max(1, token_budget - observation_reserved),
        include_skills=include_skills,
        skill_limit=skill_limit,
    )
    skill_reserved = estimate_tokens(skill_text) + 1 if skill_text else 0
    reserved = observation_reserved + skill_reserved
    observation_count = sum(
        len(
            GraphObservationRepository(service.store).scope_observations(
                scope=scope, tenant_id=observation_tenant_id
            )
        )
        for scope in scope_allowlist
    )
    result = service.query_for_context(
        query=query,
        token_budget=max(1, token_budget - reserved),
        limit=100 + min(observation_count, 400),
        output_format=output_format,
        retrieval_mode=retrieval_mode,
        trust_mode=trust_mode,
        scope_allowlist=scope_allowlist,
    )
    ordinary_rows = [
        row for row in result.rows if getattr(row["claim"], "claim_type", None) != "observation"
    ][:100]
    if include_skills:
        ordinary_rows = [row for row in ordinary_rows if not is_skill(row["claim"])]
    if len(ordinary_rows) != len(result.rows):
        result = pack_context(
            ordinary_rows,
            token_budget=max(1, token_budget - reserved),
            output_format=output_format,
        )
    sections = [text for text in (result.output, observation_text, skill_text) if text]
    output = "\n\n".join(sections)
    return ContextBundle(
        output=output,
        rows=result.rows,
        skills=skills,
        tokens_used=result.tokens_used + reserved,
        token_budget=token_budget,
        output_format=result.format,
        observations=observations,
    )


def _selected_skill_text(
    service: Any,
    query: str,
    *,
    scopes: list[str],
    total_budget: int,
    include_skills: bool,
    skill_limit: int,
) -> tuple[str, tuple[dict[str, Any], ...]]:
    if not include_skills:
        return "", ()
    bounded_limit = max(1, min(int(skill_limit), 10))
    candidates = recall_skills(
        service,
        query,
        scope_allowlist=scopes,
        limit=bounded_limit,
    )
    skill_budget = min(1200, max(128, total_budget // 3), max(1, total_budget // 2))
    return pack_approved_skills(candidates, token_budget=skill_budget)


__all__ = ["ContextBundle", "pack_approved_skills", "query_context_bundle"]
