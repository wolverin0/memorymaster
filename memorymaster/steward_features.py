"""Feature extraction for the steward promotion classifier (task #129, v2).

Shape must stay stable across training and serving. Any change here requires
bumping ``FEATURE_VERSION`` and retraining the artifact. See
``artifacts/spec-steward-classifier-2026-04-23.md`` and
``artifacts/steward-classifier-feature-audit-2026-04-23.md``.

v2 adds claim-intrinsic quality signals (text shape, citation depth, entity
richness, link fan-out). v1 features that leaked the chronological split
(``session_age_days``) are kept but the learner downweights them with the
additional evidence.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

FEATURE_VERSION = "v2"

FEATURE_KEYS: tuple[str, ...] = (
    # v1 core
    "n_citations",
    "source_agent_trust",
    "scope_quality",
    "conflict_delta",
    "session_age_days",
    "access_count",
    "has_verbatim_excerpt",
    "claim_type_bin",
    "sensitivity_flagged",
    # v2 text quality
    "text_length",
    "word_count",
    "sentence_count",
    "has_url",
    "has_code_fence",
    "has_file_path",
    # v2 cross-claim links
    "n_related_claims",
    "n_supersedes",
    "n_superseded_by",
    # v2 entity features
    "has_entity",
    # v2 citation depth
    "citation_has_locator",
    "citation_distinct_sources",
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

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
# file path with at least one slash + typical extension OR Windows-style drive
_FILE_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|[./~]?[\w\-.]+[\\/])[\w\-./\\]*\.(?:py|js|ts|tsx|jsx|md|"
    r"json|yml|yaml|toml|sh|sql|html|css|go|rs|java|rb|cpp|c|h|txt)\b"
)
_CODE_FENCE_RE = re.compile(r"```|`[^`]{2,}`")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+\s+|[.!?]$")


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


def _text_quality(text: str | None) -> dict[str, float]:
    """Cheap deterministic text-quality signals. All counts capped to avoid
    outlier-driven scale issues in linear models."""
    if not text:
        return {
            "text_length": 0.0,
            "word_count": 0.0,
            "sentence_count": 0.0,
            "has_url": 0.0,
            "has_code_fence": 0.0,
            "has_file_path": 0.0,
        }
    return {
        "text_length": float(min(len(text), 4000)),
        "word_count": float(min(len(text.split()), 800)),
        "sentence_count": float(min(len([s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]), 40)),
        "has_url": 1.0 if _URL_RE.search(text) else 0.0,
        "has_code_fence": 1.0 if _CODE_FENCE_RE.search(text) else 0.0,
        "has_file_path": 1.0 if _FILE_PATH_RE.search(text) else 0.0,
    }


def _safe_count(conn: sqlite3.Connection, sql: str, params: tuple) -> int:
    """Run a COUNT query but swallow ``OperationalError`` when the referenced
    table/column is absent (happens in unit-test in-memory DBs that only
    create a minimal schema)."""
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int((row or [0])[0])


def _cross_claim(claim_id: int, supersedes_id: Any, conn: sqlite3.Connection) -> dict[str, float]:
    """Counts of sibling claims via ``claim_links`` (``relates_to``) plus
    supersedes/superseded-by via the ``claims.supersedes_claim_id`` and
    ``claims.replaced_by_claim_id`` columns. Missing tables/columns degrade
    to 0 — unit tests that mock the schema don't need to know about links."""
    if not claim_id:
        return {"n_related_claims": 0.0, "n_supersedes": 0.0, "n_superseded_by": 0.0}

    n_related = _safe_count(
        conn,
        "SELECT COUNT(*) FROM claim_links "
        "WHERE (source_id = ? OR target_id = ?) AND link_type = 'relates_to'",
        (claim_id, claim_id),
    )

    # "how many claims does this one replace?" — every claim whose
    # replaced_by_claim_id points here counts.
    n_supersedes = _safe_count(
        conn,
        "SELECT COUNT(*) FROM claims WHERE replaced_by_claim_id = ?",
        (claim_id,),
    )
    if supersedes_id:
        # also count the explicit pointer
        n_supersedes = max(n_supersedes, 1)

    # "how many claims replaced this one?" — every newer claim with this id
    # in its supersedes_claim_id counts.
    n_superseded_by = _safe_count(
        conn,
        "SELECT COUNT(*) FROM claims WHERE supersedes_claim_id = ?",
        (claim_id,),
    )

    return {
        "n_related_claims": float(min(n_related, 20)),
        "n_supersedes": float(min(n_supersedes, 10)),
        "n_superseded_by": float(min(n_superseded_by, 10)),
    }


def _citation_depth(claim_id: int, conn: sqlite3.Connection) -> dict[str, float]:
    """Richness signals on the citations table beyond mere count. Missing
    columns in test-schemas degrade to 0 via ``_safe_count``."""
    if not claim_id:
        return {"citation_has_locator": 0.0, "citation_distinct_sources": 0.0}
    try:
        has_locator = 1 if conn.execute(
            "SELECT 1 FROM citations WHERE claim_id = ? AND locator IS NOT NULL "
            "AND LENGTH(TRIM(locator)) > 0 LIMIT 1", (claim_id,),
        ).fetchone() else 0
    except sqlite3.OperationalError:
        has_locator = 0
    distinct_sources = _safe_count(
        conn,
        "SELECT COUNT(DISTINCT source) FROM citations WHERE claim_id = ?",
        (claim_id,),
    )
    return {
        "citation_has_locator": float(has_locator),
        "citation_distinct_sources": float(min(distinct_sources, 10)),
    }


def extract_features(claim: Any, conn: sqlite3.Connection) -> dict[str, float]:
    """Return the v2 feature dict for a single claim. ``claim`` may be a dict,
    ``sqlite3.Row``, or dataclass with standard Claim fields (``id``,
    ``text``, ``subject``, ``predicate``, ``object_value``, ``scope``,
    ``created_at``, ``access_count``, ``claim_type``, ``source_agent``,
    ``supersedes_claim_id``, ``entity_id``)."""
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
        cross = _cross_claim(cid, c.get("supersedes_claim_id"), conn)
        cite_depth = _citation_depth(cid, conn)
    else:
        n_citations = has_excerpt = sensitivity = 0
        conflict_delta = 0.0
        cross = {"n_related_claims": 0.0, "n_supersedes": 0.0, "n_superseded_by": 0.0}
        cite_depth = {"citation_has_locator": 0.0, "citation_distinct_sources": 0.0}

    txt = _text_quality(c.get("text"))
    has_entity = 1.0 if c.get("entity_id") else 0.0

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
        # v2
        **txt,
        **cross,
        "has_entity": has_entity,
        **cite_depth,
    }


def feature_vector(features: dict[str, float]) -> list[float]:
    return [float(features[k]) for k in FEATURE_KEYS]
