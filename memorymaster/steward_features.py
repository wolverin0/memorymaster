"""Feature extraction for the steward promotion classifier (task #129).

Shape must stay stable across training and serving. Any change here requires
bumping ``FEATURE_VERSION`` and retraining the artifact. See
``artifacts/spec-steward-classifier-2026-04-23.md``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

FEATURE_VERSION = "v1"

FEATURE_KEYS: tuple[str, ...] = (
    "n_citations",
    "source_agent_trust",
    "scope_quality",
    "conflict_delta",
    "session_age_days",
    "access_count",
    "has_verbatim_excerpt",
    "claim_type_bin",
    "sensitivity_flagged",
)

_TRUSTED_AGENTS: frozenset[str] = frozenset({
    "claude-session", "codex-session", "claude-auto-dream",
})

# claim_type -> bin. Unknown/None -> 0 ("other"). gotcha folds into bug;
# architecture/environment fold into decision/constraint respectively.
_CLAIM_TYPE_BINS: dict[str, int] = {
    "bug": 1, "gotcha": 1,
    "decision": 2, "architecture": 2,
    "constraint": 3, "environment": 3,
    "reference": 4,
}


def _as_dict(claim: Any) -> dict[str, Any]:
    if isinstance(claim, dict):
        return claim
    if isinstance(claim, sqlite3.Row):
        return {k: claim[k] for k in claim.keys()}
    if hasattr(claim, "__dict__"):
        return {k: v for k, v in vars(claim).items() if not k.startswith("_")}
    if hasattr(claim, "__slots__"):
        return {k: getattr(claim, k, None) for k in claim.__slots__}
    raise TypeError(f"Unsupported claim type: {type(claim)!r}")


def _score_scope(scope: str | None) -> float:
    """Bare ``project`` is a red flag per audit — ingestor failed to set a
    proper project:x scope."""
    if not scope:
        return 0.0
    s = scope.strip().lower()
    if s == "global":
        return 1.0
    if s.startswith("project:"):
        return 0.8
    if s == "project":
        return 0.2
    return 0.4


def _score_source_agent(source_agent: str | None) -> float:
    if not source_agent:
        return 0.1
    if source_agent in _TRUSTED_AGENTS:
        return 1.0
    if source_agent.endswith("-session") or source_agent.endswith("-hook"):
        return 0.6
    return 0.3


def _age_days(created_at: str | None) -> float:
    if not created_at:
        return 0.0
    try:
        ts = created_at.replace("Z", "+00:00") if created_at.endswith("Z") else created_at
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
    except (ValueError, TypeError):
        return 0.0


def _conflict_delta(claim: dict[str, Any], conn: sqlite3.Connection) -> float:
    """Net disagreement on the (subject, predicate, scope) tuple the validator
    already uses. Missing parts short-circuit to 0."""
    subject = claim.get("subject")
    predicate = claim.get("predicate")
    scope = claim.get("scope")
    object_value = claim.get("object_value")
    if not (subject and predicate and scope):
        return 0.0
    rows = conn.execute(
        "SELECT object_value, status FROM claims WHERE subject = ? AND predicate = ? "
        "AND scope = ? AND id != ? AND status IN ('confirmed', 'candidate')",
        (subject, predicate, scope, claim.get("id") or -1),
    ).fetchall()
    agreeing = conflicting = 0
    for obj, _status in rows:
        if not obj or not object_value:
            continue
        if obj == object_value:
            agreeing += 1
        else:
            conflicting += 1
    return float(conflicting - agreeing)


def _sensitivity_flagged(claim_id: int, conn: sqlite3.Connection) -> int:
    """Any audit event OR 'sensitiv...' mention in event details flips the flag."""
    row = conn.execute(
        "SELECT 1 FROM events WHERE claim_id = ? AND (event_type = 'audit' "
        "OR LOWER(COALESCE(details, '')) LIKE '%sensitiv%') LIMIT 1",
        (claim_id,),
    ).fetchone()
    return 1 if row else 0


def extract_features(claim: Any, conn: sqlite3.Connection) -> dict[str, float]:
    """Return the v1 feature dict for a single claim. ``claim`` may be a dict,
    ``sqlite3.Row``, or dataclass with standard Claim fields (``id``,
    ``subject``, ``predicate``, ``object_value``, ``scope``, ``created_at``,
    ``access_count``, ``claim_type``, ``source_agent``)."""
    c = _as_dict(claim)
    cid = int(c.get("id") or 0)
    if cid:
        n_citations = int(
            (conn.execute(
                "SELECT COUNT(*) FROM citations WHERE claim_id = ?", (cid,)
            ).fetchone() or [0])[0]
        )
        has_excerpt = 1 if conn.execute(
            "SELECT 1 FROM citations WHERE claim_id = ? AND excerpt IS NOT NULL "
            "AND LENGTH(TRIM(excerpt)) > 0 LIMIT 1", (cid,),
        ).fetchone() else 0
        conflict_delta = _conflict_delta(c, conn)
        sensitivity = _sensitivity_flagged(cid, conn)
    else:
        n_citations = has_excerpt = sensitivity = 0
        conflict_delta = 0.0

    return {
        "n_citations": float(n_citations),
        "source_agent_trust": _score_source_agent(c.get("source_agent")),
        "scope_quality": _score_scope(c.get("scope")),
        "conflict_delta": float(conflict_delta),
        "session_age_days": _age_days(c.get("created_at")),
        "access_count": float(c.get("access_count") or 0),
        "has_verbatim_excerpt": float(has_excerpt),
        "claim_type_bin": float(_CLAIM_TYPE_BINS.get((c.get("claim_type") or "").lower(), 0)),
        "sensitivity_flagged": float(sensitivity),
    }


def feature_vector(features: dict[str, float]) -> list[float]:
    return [float(features[k]) for k in FEATURE_KEYS]
