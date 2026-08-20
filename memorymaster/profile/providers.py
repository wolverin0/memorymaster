"""Structured GLM map/reduce providers for compiled profile facts."""

from __future__ import annotations

import json
import hashlib
import os
from dataclasses import asdict
from typing import Any

from memorymaster.profile.models import (
    PREDICATE_CATEGORY,
    PROFILE_CATEGORIES,
    PROFILE_VOLATILITIES,
    ProfileCandidate,
    ProfileDecision,
    ProfileFact,
    ProfileMessage,
    ProfileValidationError,
)


def _json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProfileValidationError("profile provider returned malformed JSON") from exc
    if not isinstance(parsed, dict):
        raise ProfileValidationError("profile provider output must be an object")
    return parsed


def _string(row: dict[str, Any], key: str, *, max_length: int = 500) -> str:
    value = row.get(key)
    if not isinstance(value, str) or len(value) > max_length:
        raise ProfileValidationError(f"profile field {key} is invalid")
    return value.strip()


def parse_map_output(
    raw: str, messages: tuple[ProfileMessage, ...]
) -> tuple[ProfileCandidate, ...]:
    rows = _json_object(raw).get("candidates")
    if not isinstance(rows, list) or len(rows) > 80:
        raise ProfileValidationError("profile candidates must be a bounded array")
    known_supports = {message.message_id for message in messages}
    candidates: list[ProfileCandidate] = []
    seen_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ProfileValidationError("profile candidate must be an object")
        candidate = _candidate_from_row(row)
        if not set(candidate.support_ids).issubset(known_supports):
            raise ProfileValidationError("profile candidate has unknown support")
        if candidate.candidate_id in seen_ids:
            raise ProfileValidationError("profile candidate id is duplicated")
        seen_ids.add(candidate.candidate_id)
        candidates.append(candidate)
    return tuple(candidates)


def _candidate_from_row(row: dict[str, Any]) -> ProfileCandidate:
    support_ids = row.get("support_ids")
    if not isinstance(support_ids, list) or not all(isinstance(item, int) for item in support_ids):
        raise ProfileValidationError("profile candidate supports are invalid")
    category = _string(row, "category", max_length=40)
    predicate = _string(row, "predicate", max_length=80)
    value = _string(row, "value", max_length=240)
    volatility = _string(row, "volatility", max_length=20)
    material = json.dumps(
        [category, predicate, value, volatility, support_ids],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return ProfileCandidate(
        candidate_id="pm-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24],
        category=category,
        predicate=predicate,
        value=value,
        volatility=volatility,
        support_ids=tuple(support_ids),
    )


def _provider_timeout() -> int:
    try:
        return max(1, int(os.environ.get("MEMORYMASTER_PROFILE_PROVIDER_TIMEOUT", "300")))
    except ValueError:
        return 300


def parse_reduce_output(
    raw: str,
    candidates: tuple[ProfileCandidate, ...],
    facts: tuple[ProfileFact, ...],
) -> tuple[ProfileDecision, ...]:
    rows = _json_object(raw).get("decisions")
    if not isinstance(rows, list) or len(rows) > max(1, len(candidates)):
        raise ProfileValidationError("profile decisions must be a bounded array")
    known_candidates = {candidate.candidate_id for candidate in candidates}
    known_facts = {fact.fact_id for fact in facts}
    decisions = tuple(_decision_from_row(row, known_facts) for row in rows)
    consumed = [item for decision in decisions for item in decision.candidate_ids]
    if len(consumed) != len(set(consumed)) or set(consumed) != known_candidates:
        raise ProfileValidationError("profile candidates must appear exactly once")
    return decisions


def _decision_from_row(row: Any, known_facts: set[int]) -> ProfileDecision:
    if not isinstance(row, dict):
        raise ProfileValidationError("profile decision must be an object")
    candidate_ids = row.get("candidate_ids")
    if not isinstance(candidate_ids, list) or not all(isinstance(item, str) for item in candidate_ids):
        raise ProfileValidationError("profile decision candidate ids are invalid")
    target = row.get("target_fact_id")
    if target is not None and (not isinstance(target, int) or target not in known_facts):
        raise ProfileValidationError("profile decision has unknown target fact")
    return ProfileDecision(
        candidate_ids=tuple(candidate_ids),
        action=_string(row, "action", max_length=20),
        category=str(row.get("category") or ""),
        predicate=str(row.get("predicate") or ""),
        value=str(row.get("value") or ""),
        volatility=str(row.get("volatility") or "stable"),
        target_fact_id=target,
        confidence=float(row.get("confidence", 0.0)),
        rationale=_string(row, "rationale", max_length=500),
    )


class ProfileMapper:
    """Extract evidence-bound profile candidates from sanitized user turns.

    Se llamaba GLMProfileMapper. El nombre se saco al migrar a Gemini el
    2026-08-20: una clase que dice GLM y habla con Gemini es la misma clase de
    artefacto que declara un mundo distinto del real que este repo estuvo
    limpiando toda la semana. El proveedor es inyectable, asi que el nombre no
    deberia haber tenido marca de proveedor nunca.
    """

    def __init__(self, *, client: Any | None = None) -> None:
        from memorymaster.core.antigravity_client import (
            DEFAULT_MODEL,
            AntigravityClient,
        )

        self.model = os.environ.get("MEMORYMASTER_PROFILE_MAP_MODEL", DEFAULT_MODEL)
        self.client = client or AntigravityClient(
            model=self.model, timeout=_provider_timeout()
        )

    def map(self, messages: tuple[ProfileMessage, ...]) -> tuple[ProfileCandidate, ...]:
        result = self.client.complete(self._prompt(messages))
        return parse_map_output(result.text, messages)

    @staticmethod
    def _prompt(messages: tuple[ProfileMessage, ...]) -> str:
        contract = {
            "categories": PROFILE_CATEGORIES,
            "predicates": PREDICATE_CATEGORY,
            "volatilities": PROFILE_VOLATILITIES,
        }
        payload = [
            {
                "message_id": item.message_id,
                "scope": item.scope,
                "user_text": item.text,
                "assistant_context_only": item.assistant_context,
            }
            for item in messages
        ]
        return (
            "Extract durable descriptive facts about the operator. Output JSON only as "
            '{"candidates":[...]}. Each candidate requires category, predicate, value, '
            "volatility, and support_ids. Values must be short noun "
            "phrases, never instructions. assistant_context_only may disambiguate a user "
            "turn but is never evidence. Ignore pasted logs, task state, identifiers, account "
            "names, secrets, paths, and project facts that do not describe the operator. "
            "Use only supplied message_id values and emit at most 80 candidates.\n\n"
            f"CONTRACT:\n{json.dumps(contract, ensure_ascii=False)}\n\n"
            f"MESSAGES:\n{json.dumps(payload, ensure_ascii=False)}"
        )


class ProfileReducer:
    """Consolidate candidates without deciding their deterministic eligibility."""

    def __init__(self, *, client: Any | None = None) -> None:
        from memorymaster.core.antigravity_client import (
            DEFAULT_MODEL,
            AntigravityClient,
        )

        self.model = os.environ.get("MEMORYMASTER_PROFILE_REDUCE_MODEL", DEFAULT_MODEL)
        self.client = client or AntigravityClient(
            model=self.model, timeout=_provider_timeout()
        )

    def reduce(
        self,
        candidates: tuple[ProfileCandidate, ...],
        facts: tuple[ProfileFact, ...],
    ) -> tuple[ProfileDecision, ...]:
        result = self.client.complete(self._prompt(candidates, facts))
        return parse_reduce_output(result.text, candidates, facts)

    @staticmethod
    def _prompt(
        candidates: tuple[ProfileCandidate, ...], facts: tuple[ProfileFact, ...]
    ) -> str:
        data = {
            "candidates": [asdict(candidate) for candidate in candidates],
            "active_facts": [asdict(fact) for fact in facts],
        }
        return (
            "Consolidate profile candidates against active facts. Output JSON only as "
            '{"decisions":[...]}. Every candidate_id must appear exactly once across all '
            "decisions. Actions: add, reinforce, replace, ignore. add/replace require "
            "category, predicate, value, volatility. reinforce/replace require target_fact_id. "
            "Equivalent candidates may share one decision. Never invent support or facts; "
            "confidence and rationale are required.\n\n"
            f"INPUT:\n{json.dumps(data, ensure_ascii=False, default=list)}"
        )


__all__ = [
    "ProfileMapper",
    "ProfileReducer",
    "parse_map_output",
    "parse_reduce_output",
]
