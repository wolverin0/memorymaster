"""Governed retrieval request planning.

The planner separates caller intent from the existing retrieval machinery.  It
is deliberately pure and immutable so every public surface can share the same
trust/status and transport-containment decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


_TRUST_MODES = frozenset({"trusted", "exploratory"})
_RETRIEVAL_MODES = frozenset({"legacy", "hybrid", "qdrant"})
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "does", "for",
        "from", "how", "in", "is", "it", "of", "on", "or", "that", "the",
        "this", "to", "what", "when", "where", "which", "who", "why", "with",
    }
)


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    query_text: str
    limit: int = 20
    trust_mode: str = "trusted"
    retrieval_mode: str = "legacy"
    include_stale: bool | None = None
    include_conflicted: bool | None = None
    include_candidates: bool | None = None
    allow_sensitive: bool = False
    scope_allowlist: tuple[str, ...] | None = None
    requesting_agent: str | None = None
    query_type: str | None = None
    retrieval_profile: str | None = None
    qdrant_candidate_reads: bool = False


@dataclass(frozen=True, slots=True)
class RetrievalPlan:
    query_text: str
    search_text: str
    limit: int
    trust_mode: str
    statuses: tuple[str, ...]
    requested_mode: str
    effective_mode: str
    containment_reason: str | None
    allow_sensitive: bool
    scope_allowlist: tuple[str, ...] | None
    requesting_agent: str | None
    query_type: str | None
    retrieval_profile: str | None


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    plan: RetrievalPlan
    rows: tuple[dict[str, object], ...]


def _normalize_search_text(query_text: str) -> str:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[\w-]+", query_text.lower(), flags=re.UNICODE):
        if len(token) < 2 or token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) == 12:
            break
    # Short keyword searches already have useful AND semantics. Conversational
    # prompts accumulate filler and modifiers, so requiring every term causes
    # false negatives; broaden only those longer requests.
    if len(tokens) < 5:
        return query_text.strip()
    return " OR ".join(tokens) or query_text.strip()


def _statuses(request: RetrievalRequest) -> tuple[str, ...]:
    if request.trust_mode == "trusted":
        if any(
            value is True
            for value in (
                request.include_stale,
                request.include_conflicted,
                request.include_candidates,
            )
        ):
            raise ValueError("Expanded statuses require trust_mode='exploratory'.")
        return ("confirmed",)

    statuses = ["confirmed"]
    choices = (
        ("stale", request.include_stale),
        ("conflicted", request.include_conflicted),
        ("candidate", request.include_candidates),
    )
    statuses.extend(status for status, enabled in choices if enabled is not False)
    return tuple(statuses)


def build_retrieval_plan(request: RetrievalRequest) -> RetrievalPlan:
    """Validate and resolve a retrieval request without performing I/O."""
    if request.limit <= 0:
        raise ValueError("Retrieval limit must be positive.")
    if request.trust_mode not in _TRUST_MODES:
        raise ValueError(f"Unsupported trust mode: {request.trust_mode}")
    if request.retrieval_mode not in _RETRIEVAL_MODES:
        raise ValueError(f"Unsupported retrieval mode: {request.retrieval_mode}")

    effective_mode = request.retrieval_mode
    containment_reason = None
    if request.retrieval_mode == "qdrant" and not request.qdrant_candidate_reads:
        effective_mode = "legacy"
        containment_reason = (
            "qdrant retrieval is quarantined pending governed ID rehydration"
        )

    return RetrievalPlan(
        query_text=request.query_text,
        search_text=_normalize_search_text(request.query_text),
        limit=request.limit,
        trust_mode=request.trust_mode,
        statuses=_statuses(request),
        requested_mode=request.retrieval_mode,
        effective_mode=effective_mode,
        containment_reason=containment_reason,
        allow_sensitive=request.allow_sensitive,
        scope_allowlist=request.scope_allowlist,
        requesting_agent=request.requesting_agent,
        query_type=request.query_type,
        retrieval_profile=request.retrieval_profile,
    )
