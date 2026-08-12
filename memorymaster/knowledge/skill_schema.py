"""Strict personal-skill-v1 schema, hashing, parsing, and rendering.

Skill payloads are stored as ordinary governed claims; this module keeps their
JSON deterministic and rejects malformed reviewer output before persistence.
The content hash covers executable skill content, not mutable lineage metadata,
so replay and update matching remain stable.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping


SKILL_CLAIM_TYPE = "skill"
SKILL_PREDICATE = "applies_when"
SKILL_SCHEMA = "personal-skill-v1"
QUALITY_DIMENSIONS = ("recurrence", "reusability", "executability", "validation", "safety")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_LIST_FIELDS = (
    "inputs",
    "prerequisites",
    "workflow",
    "decision_rules",
    "validation",
    "pitfalls",
    "recovery",
)
_TEXT_FIELDS = ("title", "when_to_use", "when_not_to_use", "expected_output")
_CONTENT_FIELDS = ("schema", "slug", *_TEXT_FIELDS, *_LIST_FIELDS)
_ALLOWED_FIELDS = {
    *_CONTENT_FIELDS,
    "quality_scores",
    "supporting_claim_ids",
    "expected_parent_claim_id",
    "expected_parent_version",
    "skill_version",
    "content_sha256",
}


class SkillValidationError(ValueError):
    """Reviewer output or persisted skill payload failed closed."""


def _text(value: object, field: str, *, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SkillValidationError(f"{field} must be a non-empty string")
    result = " ".join(value.strip().split())
    if len(result) > maximum:
        raise SkillValidationError(f"{field} exceeds {maximum} characters")
    return result


def _string_list(value: object, field: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise SkillValidationError(f"{field} must be a list")
    if len(value) > 50:
        raise SkillValidationError(f"{field} exceeds 50 items")
    result = [_text(item, f"{field} item", maximum=1000) for item in value]
    if required and not result:
        raise SkillValidationError(f"{field} must contain at least one item")
    return result


def _positive_ids(value: object, field: str, *, required: bool = False) -> list[int]:
    if not isinstance(value, list):
        raise SkillValidationError(f"{field} must be a list")
    if any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in value):
        raise SkillValidationError(f"{field} must contain positive integers")
    result = sorted(set(value))
    if len(result) > 50:
        raise SkillValidationError(f"{field} exceeds 50 items")
    if required and not result:
        raise SkillValidationError(f"{field} must contain supporting evidence")
    return result


def _quality_scores(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(QUALITY_DIMENSIONS):
        raise SkillValidationError(f"quality_scores must contain exactly {', '.join(QUALITY_DIMENSIONS)}")
    scores: dict[str, int] = {}
    for name in QUALITY_DIMENSIONS:
        score = value[name]
        if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 20:
            raise SkillValidationError(f"quality_scores.{name} must be an integer from 0 to 20")
        if score < 12:
            raise SkillValidationError(f"quality_scores.{name} must be at least 12")
        scores[name] = score
    if sum(scores.values()) < 72:
        raise SkillValidationError("quality_scores total must be at least 72")
    return scores


def _optional_positive_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SkillValidationError(f"{field} must be a positive integer")
    return value


def _normalized_content(payload: Mapping[str, Any]) -> dict[str, Any]:
    content = {field: payload[field] for field in _CONTENT_FIELDS}
    return content


def skill_content_sha256(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(_normalized_content(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_skill_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SkillValidationError("skill payload must be an object")
    unknown = sorted(set(payload) - _ALLOWED_FIELDS)
    if unknown:
        raise SkillValidationError(f"unknown skill fields: {', '.join(unknown)}")
    if payload.get("schema") != SKILL_SCHEMA:
        raise SkillValidationError(f"schema must be {SKILL_SCHEMA}")
    slug = _text(payload.get("slug"), "slug", maximum=80).lower()
    if not _SLUG_RE.fullmatch(slug):
        raise SkillValidationError("slug must contain lowercase words separated by hyphens")
    result: dict[str, Any] = {"schema": SKILL_SCHEMA, "slug": slug}
    result.update({field: _text(payload.get(field), field) for field in _TEXT_FIELDS})
    for field in _LIST_FIELDS:
        result[field] = _string_list(payload.get(field), field, required=field in {"workflow", "validation"})
    result["quality_scores"] = _quality_scores(payload.get("quality_scores"))
    result["supporting_claim_ids"] = _positive_ids(payload.get("supporting_claim_ids", []), "supporting_claim_ids")
    _validate_version_fields(payload, result)
    expected_hash = skill_content_sha256(result)
    supplied_hash = payload.get("content_sha256")
    if supplied_hash is not None and supplied_hash != expected_hash:
        raise SkillValidationError("content_sha256 does not match canonical skill content")
    result["content_sha256"] = expected_hash
    return result


def _validate_version_fields(payload: Mapping[str, Any], result: dict[str, Any]) -> None:
    parent_id = _optional_positive_int(payload.get("expected_parent_claim_id"), "expected_parent_claim_id")
    parent_version = _optional_positive_int(payload.get("expected_parent_version"), "expected_parent_version")
    if (parent_id is None) != (parent_version is None):
        raise SkillValidationError("expected parent claim and version must be provided together")
    skill_version = _optional_positive_int(payload.get("skill_version", 1), "skill_version")
    result["expected_parent_claim_id"] = parent_id
    result["expected_parent_version"] = parent_version
    result["skill_version"] = skill_version


def build_skill_fields(payload: Mapping[str, Any], *, supporting_claim_ids: list[int]) -> dict[str, Any]:
    merged = dict(payload)
    merged["supporting_claim_ids"] = _positive_ids(
        supporting_claim_ids, "supporting_claim_ids", required=True
    )
    skill = validate_skill_payload(merged)
    return {
        "text": f"Skill {skill['title']}: {skill['when_to_use']}",
        "claim_type": SKILL_CLAIM_TYPE,
        "subject": skill["slug"],
        "predicate": SKILL_PREDICATE,
        "object_value": json.dumps(skill, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    }


def is_skill(claim: Any) -> bool:
    return getattr(claim, "claim_type", None) == SKILL_CLAIM_TYPE


def parse_skill(claim: Any) -> dict[str, Any] | None:
    if not is_skill(claim):
        return None
    try:
        raw = json.loads(getattr(claim, "object_value", "") or "")
        skill = validate_skill_payload(raw)
    except (json.JSONDecodeError, TypeError, SkillValidationError):
        return None
    skill.update(
        {
            "claim_id": getattr(claim, "id", None),
            "status": getattr(claim, "status", None),
            "scope": getattr(claim, "scope", None),
            "claim_version": getattr(claim, "version", None),
            "citations": list(getattr(claim, "citations", []) or []),
        }
    )
    return skill


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_skill_markdown(claim: Any) -> str:
    skill = parse_skill(claim)
    if skill is None:
        raise SkillValidationError("claim is not a valid personal-skill-v1 skill")
    citations = sorted(
        f"{item.source}:{item.locator or ''}" for item in skill["citations"]
    )
    lines = _skill_header(skill, citations)
    lines.extend(_skill_body(skill))
    return "\n".join(lines).rstrip() + "\n"


def _skill_header(skill: Mapping[str, Any], citations: list[str]) -> list[str]:
    description = f"{skill['title']}. Use when {skill['when_to_use']}"
    return [
        "---",
        f"name: {_yaml_string(skill['slug'])}",
        f"description: {_yaml_string(description)}",
        f"memorymaster_claim_id: {skill['claim_id']}",
        f"memorymaster_scope: {_yaml_string(skill['scope'])}",
        f"memorymaster_content_sha256: {_yaml_string(skill['content_sha256'])}",
        f"memorymaster_skill_version: {skill['skill_version']}",
        f"memorymaster_citations: {json.dumps(citations, ensure_ascii=False)}",
        "---",
        "",
    ]


def _section(title: str, values: list[str]) -> list[str]:
    if not values:
        return []
    return [f"## {title}", "", *(f"- {value}" for value in values), ""]


def _skill_body(skill: Mapping[str, Any]) -> list[str]:
    lines = [f"# {skill['title']}", "", "## Use", "", skill["when_to_use"], "", "## Do not use", "", skill["when_not_to_use"], ""]
    lines.extend(_section("Inputs", skill["inputs"]))
    lines.extend(_section("Prerequisites", skill["prerequisites"]))
    lines.extend(["## Workflow", "", *(f"{index}. {step}" for index, step in enumerate(skill["workflow"], 1)), ""])
    lines.extend(_section("Decision rules", skill["decision_rules"]))
    lines.extend(["## Expected output", "", skill["expected_output"], ""])
    lines.extend(_section("Validation", skill["validation"]))
    lines.extend(_section("Pitfalls", skill["pitfalls"]))
    lines.extend(_section("Recovery", skill["recovery"]))
    return lines
