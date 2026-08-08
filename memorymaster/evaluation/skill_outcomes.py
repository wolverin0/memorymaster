"""Strict content-free execution outcomes for governed skill evaluation."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from memorymaster.core.security import is_sensitive_claim, scan_text_for_findings
from memorymaster.knowledge.skill_schema import parse_skill


REPORT_SCHEMA = "memorymaster.skill-outcomes.v1"
_OUTCOMES = {"success", "failure", "ambiguous"}
_RESULTS = {"passed", "failed", "not_checked"}
_FIELDS = {
    "execution_ref", "skill_claim_id", "skill_version", "outcome", "observed_at",
    "consumer_profile", "model_profile", "tool_name", "tool_schema_sha256",
    "activation_matched", "termination_result", "validation_result", "metrics",
}
_METRICS = {"elapsed_ms": 3_600_000, "attempts": 1000, "tool_calls": 1000}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")


class SkillOutcomeValidationError(ValueError):
    """An execution observation failed closed before evaluation."""


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise SkillOutcomeValidationError(f"{field} must be a bounded identifier")
    if scan_text_for_findings(value):
        raise SkillOutcomeValidationError(f"{field} contains sensitive data")
    return value


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise SkillOutcomeValidationError("observed_at must be an ISO-8601 timestamp")
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SkillOutcomeValidationError("observed_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise SkillOutcomeValidationError("observed_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SkillOutcomeValidationError(f"{field} must be a positive integer")
    return value


def _enum(value: object, field: str, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise SkillOutcomeValidationError(f"{field} is unsupported")
    return value


def _metrics(value: object) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        raise SkillOutcomeValidationError("metrics must be an object")
    unknown = sorted(set(value) - set(_METRICS))
    if unknown:
        raise SkillOutcomeValidationError(f"unknown metrics: {', '.join(unknown)}")
    result: dict[str, int | float] = {}
    for name in sorted(value):
        metric = value[name]
        if isinstance(metric, bool) or not isinstance(metric, (int, float)):
            raise SkillOutcomeValidationError(f"metrics.{name} must be numeric")
        if metric < 0 or metric > _METRICS[name]:
            raise SkillOutcomeValidationError(f"metrics.{name} is outside the supported range")
        result[name] = metric
    return result


def _validate_consistency(observation: Mapping[str, Any]) -> None:
    if observation["outcome"] != "success":
        return
    if not observation["activation_matched"]:
        raise SkillOutcomeValidationError("success requires an activation match")
    if observation["termination_result"] != "passed":
        raise SkillOutcomeValidationError("success requires passed termination")
    if observation["validation_result"] != "passed":
        raise SkillOutcomeValidationError("success requires passed validation")


def _normalize(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise SkillOutcomeValidationError("observation must be an object")
    unknown = sorted(set(raw) - _FIELDS)
    missing = sorted(_FIELDS - set(raw))
    if unknown:
        raise SkillOutcomeValidationError(f"unknown observation fields: {', '.join(unknown)}")
    if missing:
        raise SkillOutcomeValidationError(f"missing observation fields: {', '.join(missing)}")
    snapshot = raw["tool_schema_sha256"]
    if not isinstance(snapshot, str) or not _SHA256.fullmatch(snapshot):
        raise SkillOutcomeValidationError("tool_schema_sha256 must be a sha256 digest")
    activation = raw["activation_matched"]
    if not isinstance(activation, bool):
        raise SkillOutcomeValidationError("activation_matched must be boolean")
    result = {
        "execution_ref": _identifier(raw["execution_ref"], "execution_ref"),
        "skill_claim_id": _positive_int(raw["skill_claim_id"], "skill_claim_id"),
        "skill_version": _positive_int(raw["skill_version"], "skill_version"),
        "outcome": _enum(raw["outcome"], "outcome", _OUTCOMES),
        "observed_at": _timestamp(raw["observed_at"]),
        "consumer_profile": _identifier(raw["consumer_profile"], "consumer_profile"),
        "model_profile": _identifier(raw["model_profile"], "model_profile"),
        "tool_name": _identifier(raw["tool_name"], "tool_name"),
        "tool_schema_sha256": snapshot.lower(),
        "activation_matched": activation,
        "termination_result": _enum(raw["termination_result"], "termination_result", _RESULTS),
        "validation_result": _enum(raw["validation_result"], "validation_result", _RESULTS),
        "metrics": _metrics(raw["metrics"]),
    }
    _validate_consistency(result)
    return result


def _observation_id(execution_ref: str) -> str:
    return hashlib.sha256(execution_ref.encode("utf-8")).hexdigest()[:24]


def _content_fingerprint(observation: Mapping[str, Any]) -> str:
    content = {key: value for key, value in observation.items() if key != "execution_ref"}
    raw = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _authorized_skill(service: Any, claim_id: int, scopes: set[str]) -> tuple[Any, dict[str, Any]] | None:
    claim = service.store.get_claim(claim_id)
    if claim is None or claim.status != "confirmed" or claim.scope not in scopes:
        return None
    if getattr(claim, "visibility", "public") != "public" or is_sensitive_claim(claim):
        return None
    skill = parse_skill(claim)
    return (claim, skill) if skill is not None else None


def _review_signal(outcome: str) -> str:
    return {
        "success": "positive_review",
        "failure": "negative_warning",
        "ambiguous": "neutral_review",
    }[outcome]


def _public_observation(observation: Mapping[str, Any], observation_id: str) -> dict[str, Any]:
    result = {key: value for key, value in observation.items() if key != "execution_ref"}
    result["observation_id"] = observation_id
    result["review_signal"] = _review_signal(str(observation["outcome"]))
    return result


def _warning(record: Mapping[str, Any]) -> dict[str, Any] | None:
    outcome = str(record["outcome"])
    if outcome == "success":
        return None
    code = "skill_execution_failed" if outcome == "failure" else "skill_execution_ambiguous"
    return {
        "observation_id": record["observation_id"],
        "skill_claim_id": record["skill_claim_id"],
        "code": code,
    }


def evaluate_skill_outcomes(
    service: Any,
    observations: Sequence[Mapping[str, Any]],
    *,
    scope_allowlist: Sequence[str],
    max_observations: int = 100,
) -> dict[str, Any]:
    if not 1 <= max_observations <= 500:
        raise SkillOutcomeValidationError("max_observations is outside the supported range")
    if len(observations) > max_observations:
        raise SkillOutcomeValidationError("observation batch exceeds max_observations")
    scopes = {scope for scope in scope_allowlist if scope}
    diagnostics = {"duplicates": 0, "unauthorized_skill": 0, "version_mismatch": 0}
    seen: dict[str, str] = {}
    accepted: list[dict[str, Any]] = []
    for raw in observations:
        normalized = _normalize(raw)
        observation_id = _observation_id(normalized["execution_ref"])
        fingerprint = _content_fingerprint(normalized)
        if observation_id in seen:
            if seen[observation_id] != fingerprint:
                raise SkillOutcomeValidationError("execution_ref collision has different content")
            diagnostics["duplicates"] += 1
            continue
        seen[observation_id] = fingerprint
        resolved = _authorized_skill(service, normalized["skill_claim_id"], scopes)
        if resolved is None:
            diagnostics["unauthorized_skill"] += 1
            continue
        _, skill = resolved
        if normalized["skill_version"] != skill["skill_version"]:
            diagnostics["version_mismatch"] += 1
            continue
        accepted.append(_public_observation(normalized, observation_id))
    accepted.sort(key=lambda row: (row["observed_at"], row["observation_id"]))
    warnings = [item for row in accepted if (item := _warning(row)) is not None]
    counts = {name: sum(row["outcome"] == name for row in accepted) for name in sorted(_OUTCOMES)}
    counts["positive_review"] = sum(row["review_signal"] == "positive_review" for row in accepted)
    counts["warnings"] = len(warnings)
    return {
        "schema_version": REPORT_SCHEMA,
        "observations": accepted,
        "warnings": warnings,
        "counts": counts,
        "diagnostics": diagnostics,
    }


def write_skill_outcome_report(report: Mapping[str, Any], path: str | Path) -> None:
    if report.get("schema_version") != REPORT_SCHEMA:
        raise SkillOutcomeValidationError("report schema is unsupported")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "REPORT_SCHEMA",
    "SkillOutcomeValidationError",
    "evaluate_skill_outcomes",
    "write_skill_outcome_report",
]
