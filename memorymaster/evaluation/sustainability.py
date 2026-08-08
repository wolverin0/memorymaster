"""Aggregate-safe stage telemetry for offline MemoryMaster evaluations."""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Sequence, TypeVar

from memorymaster.recall.context_optimizer import ContextResult, pack_context
from memorymaster.recall.planner import RetrievalRequest


REPORT_SCHEMA = "memorymaster.sustainability-report.v1"
MAX_OBSERVATIONS = 64
STAGES = (
    "retrieval",
    "graph_expansion",
    "evidence_map_back",
    "admission",
    "packing",
    "skill_recall",
    "skill_review",
    "answer_generation",
    "judge_generation",
)
CACHE_STATES = {"not_applicable", "hit", "miss", "bypass"}
TIERS = {"legacy", "low", "balanced", "high", "temporal", "procedural"}
FALLBACK_REASONS = {
    "none",
    "insufficient_evidence",
    "provider_unavailable",
    "budget_exhausted",
    "graph_unavailable",
    "cache_miss",
    "unsupported_stage",
}
CORRECTNESS_FIELDS = {"answer_correct", "citation_correct", "task_correct"}
_PROFILE = re.compile(r"^[a-z][a-z0-9+_-]{0,47}$")
_COUNT_FIELDS = (
    "content_chars_read",
    "provider_calls",
    "tool_calls",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
)
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class StageObservation:
    stage: str
    elapsed_ms: float
    content_chars_read: int = 0
    provider_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_state: str = "not_applicable"
    selected_tier: str = "legacy"
    fallback_reason: str = "none"

    def __post_init__(self) -> None:
        if self.stage not in STAGES:
            raise ValueError(f"stage must be one of {STAGES}")
        if self.cache_state not in CACHE_STATES:
            raise ValueError(f"cache_state must be one of {sorted(CACHE_STATES)}")
        if self.selected_tier not in TIERS:
            raise ValueError(f"selected_tier must be one of {sorted(TIERS)}")
        if self.fallback_reason not in FALLBACK_REASONS:
            raise ValueError(f"fallback_reason must be one of {sorted(FALLBACK_REASONS)}")
        if not isinstance(self.elapsed_ms, (int, float)) or self.elapsed_ms < 0:
            raise ValueError("elapsed_ms must be non-negative")
        for field in _COUNT_FIELDS:
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")


def measure_stage(
    stage: str,
    operation: Callable[[], T],
    *,
    clock: Callable[[], float] = time.perf_counter,
    content_sizer: Callable[[T], int] | None = None,
    provider_calls: int = 0,
    tool_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_tokens: int = 0,
    cache_state: str = "not_applicable",
    selected_tier: str = "legacy",
    fallback_reason: str = "none",
) -> tuple[T, StageObservation]:
    started = clock()
    result = operation()
    elapsed_ms = (clock() - started) * 1000.0
    content_chars = content_sizer(result) if content_sizer else 0
    observation = StageObservation(
        stage=stage,
        elapsed_ms=elapsed_ms,
        content_chars_read=content_chars,
        provider_calls=provider_calls,
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cache_state=cache_state,
        selected_tier=selected_tier,
        fallback_reason=fallback_reason,
    )
    return result, observation


def _validate_correctness(correctness: Mapping[str, bool | None] | None) -> dict[str, bool | None]:
    values = dict(correctness or {})
    unknown = set(values) - CORRECTNESS_FIELDS
    if unknown:
        raise ValueError(f"unsupported correctness fields: {sorted(unknown)}")
    if any(value is not None and not isinstance(value, bool) for value in values.values()):
        raise ValueError("correctness values must be booleans or null")
    return {field: values.get(field) for field in sorted(CORRECTNESS_FIELDS)}


def _totals(observations: Sequence[StageObservation]) -> dict[str, int | float]:
    return {
        "elapsed_ms": sum(row.elapsed_ms for row in observations),
        **{
            field: sum(getattr(row, field) for row in observations)
            for field in _COUNT_FIELDS
        },
    }


def build_report(
    observations: Sequence[StageObservation],
    *,
    profile: str,
    correctness: Mapping[str, bool | None] | None = None,
) -> dict[str, Any]:
    if not _PROFILE.fullmatch(profile):
        raise ValueError("profile must be a bounded machine-readable code")
    if len(observations) > MAX_OBSERVATIONS:
        raise ValueError(f"a request may contain at most {MAX_OBSERVATIONS} observations")
    if any(not isinstance(row, StageObservation) for row in observations):
        raise ValueError("observations must contain StageObservation values")
    return {
        "schema_version": REPORT_SCHEMA,
        "profile": profile,
        "stage_count": len(observations),
        "stages": [asdict(row) for row in observations],
        "totals": _totals(observations),
        "correctness": _validate_correctness(correctness),
    }


def _retrieval_chars(retrieval: Any) -> int:
    return sum(
        len(str(getattr(row.get("claim"), "text", "") or ""))
        for row in retrieval.rows
    )


def observe_context_query(
    service: Any,
    query: str,
    *,
    token_budget: int = 4000,
    output_format: str = "text",
    limit: int = 100,
    trust_mode: str = "trusted",
    retrieval_mode: str = "hybrid",
    scope_allowlist: list[str] | tuple[str, ...] | None = None,
    provider: str | None = None,
    selected_tier: str = "legacy",
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[ContextResult, tuple[StageObservation, ...]]:
    request = RetrievalRequest(
        query_text=query,
        limit=limit,
        trust_mode=trust_mode,
        retrieval_mode=retrieval_mode,
        scope_allowlist=tuple(scope_allowlist) if scope_allowlist else None,
    )
    retrieval, retrieval_observation = measure_stage(
        "retrieval",
        lambda: service.retrieve(request),
        clock=clock,
        content_sizer=_retrieval_chars,
        selected_tier=selected_tier,
    )
    content_chars = _retrieval_chars(retrieval)
    result, packing_observation = measure_stage(
        "packing",
        lambda: pack_context(
            list(retrieval.rows),
            token_budget=token_budget,
            output_format=output_format,
            provider=provider,
        ),
        clock=clock,
        content_sizer=lambda _result: content_chars,
        selected_tier=selected_tier,
    )
    return result, (retrieval_observation, packing_observation)


__all__ = [
    "MAX_OBSERVATIONS",
    "REPORT_SCHEMA",
    "STAGES",
    "StageObservation",
    "build_report",
    "measure_stage",
    "observe_context_query",
]
