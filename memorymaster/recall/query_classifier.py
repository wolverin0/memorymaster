"""Rule-based query classifier — routes queries to optimal retrieval."""

from __future__ import annotations

QUERY_TYPES = (
    "fact_lookup",      # "What database does pedrito use?"
    "relational",       # "What depends on PaymentService?"
    "temporal",         # "What changed last week?"
    "constraint_check", # "Are there any rules about..."
    "preference",       # "How does the user prefer..."
    "verification",     # "Is it true that..."
    "open_ended",       # "Tell me about the architecture"
)


def classify_query(query: str) -> str:
    """Classify a query into one of the QUERY_TYPES using rule-based heuristics."""
    q = query.lower().strip()

    # Temporal patterns
    if any(w in q for w in (
        "when", "last week", "yesterday", "today", "changed",
        "history", "timeline", "before", "after", "since",
    )):
        return "temporal"

    # Constraint patterns — evaluated BEFORE the verification opener heuristic so
    # that constraint questions phrased as questions (e.g. "Are there any rules
    # about X?") route to constraint_check, not verification. The verification
    # opener fires on prefixes like "are there" which would otherwise shadow the
    # per-type constraint_check retrieval profile.
    if any(w in q for w in (
        "rule", "constraint", "must", "never", "always",
        "require", "forbidden", "policy",
    )):
        return "constraint_check"

    # Verification patterns
    if q.startswith((
        "is it", "does it", "can we", "should we", "is there",
        "are there", "did we", "have we",
    )):
        return "verification"

    # Preference patterns
    if any(w in q for w in ("prefer", "like", "want", "style", "convention")):
        return "preference"

    # Relational patterns
    if any(w in q for w in (
        "depends on", "calls", "uses", "imports",
        "related to", "connected", "linked",
    )):
        return "relational"

    # Fact lookup: starts with what/where/which/who/how many
    if q.startswith(("what", "where", "which", "who", "how many", "how much")):
        return "fact_lookup"

    return "open_ended"


def recommended_retrieval_mode(query_type: str) -> str:
    """Suggest the best retrieval mode for a query type."""
    return {
        "fact_lookup": "legacy",       # Fast SQL text search
        "relational": "qdrant",        # Semantic similarity
        "temporal": "legacy",          # SQL ordering by time
        "constraint_check": "legacy",  # Keyword match
        "preference": "qdrant",        # Semantic
        "verification": "legacy",      # Keyword match
        "open_ended": "qdrant",        # Semantic exploration
    }.get(query_type, "legacy")


def profile_for_query_type(query_type: str) -> str:
    """Map a query intent to the best ranking weight profile (intent-aware
    ranking, plan 1.3). Profile names are the keys of
    ``service.RETRIEVAL_PROFILES`` (recall / precision / fresh / semantic).

    Rationale: a *temporal* query wants freshness-weighted ranking; a
    *relational*/*preference* query wants semantic (vector) weight; a
    *fact*/*constraint*/*verification* query wants precision (confidence-weighted
    exact match). *open_ended* falls back to the broad recall profile. This is
    consumed only when a caller opts in with ``retrieval_profile="auto"`` — the
    default ranking is unchanged.
    """
    return {
        "fact_lookup": "precision",
        "constraint_check": "precision",
        "verification": "precision",
        "temporal": "fresh",
        "relational": "semantic",
        "preference": "semantic",
        "open_ended": "recall",
    }.get(query_type, "recall")
