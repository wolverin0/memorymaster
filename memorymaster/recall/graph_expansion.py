"""Opt-in vector/lexical-first recall with bounded claim-graph expansion.

``MEMORYMASTER_RECALL_GRAPH_MODE`` selects the arm (default ``off``):

* ``off`` -- recall is unchanged; no expansion runs.
* ``vector_first`` -- the claim IDs that lexical/semantic recall already
  authorized are the seeds.  Existing edges (``claim_links``, ``claim_edges``
  and supported entity relations) are walked 1-2 hops under fanout,
  candidate, token and deadline caps.
* ``graph_first`` -- separate experimental arm: seeds are entities whose
  normalized alias matches the query (case-insensitive), then the same walk.

This is not a graph database and not an agent.  The graph only proposes claim
IDs; SQLite stays authoritative.  Every expanded claim and every claim that
supports its path is rehydrated from the claim store and re-authorized
(active, same tenant/scope/principal, non-sensitive, citation present,
evidence lineage not retired).  Generated observations never act as evidence.
A share of the result window is reserved for base recall, so expansion can
never displace every base result, and the graph never filters base rows.
Any failure (no seeds, no valid supports, error, timeout, unsupported store)
returns ``applied=False`` so the caller keeps its current recall.

``recall_with_graph`` is the ``MemoryService.query_rows`` entry point; it also
hosts the separate opt-in entity enrichment (``enrich_with_entities``), whose
supports are re-authorized with the same ``ClaimAuthorizer``.
"""

from __future__ import annotations

import logging
import math
import os
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from memorymaster.core.security import is_sensitive_claim, scan_text_for_findings
from memorymaster.core.temporal_policy import claim_is_temporally_current
from memorymaster.stores._storage_shared import connect_ro

logger = logging.getLogger(__name__)

MODE_ENV = "MEMORYMASTER_RECALL_GRAPH_MODE"
GRAPH_MODES = ("off", "vector_first", "graph_first")

ACTIVE_STATUS = "confirmed"
RETIRED_STATUSES = frozenset({"archived", "superseded"})
GENERATED_CLAIM_TYPES = frozenset({"observation", "skill", "summary"})
GENERATED_SOURCE_AGENTS = frozenset({"memorymaster-graph-observer"})

# claim_links types that assert a positive relation between two facts.
# ``supersedes`` and ``contradicts`` are lifecycle signals, not recall paths.
EXPANSION_LINK_TYPES = (
    "relates_to",
    "derived_from",
    "supports",
    "implements",
    "configures",
    "depends_on",
    "deployed_on",
    "owned_by",
    "tested_by",
    "documents",
    "blocks",
    "enables",
)
EXPANSION_EDGE_KINDS = ("mentions", "shares_entity")
EDGE_WEIGHTS = {"link": 1.0, "mentions": 0.9, "entity": 0.8, "shares_entity": 0.6}
HOP_DECAY = 0.5
_GRAPH_TABLES = ("claim_links", "claim_edges", "claim_entity_links", "entity_edge_supports")
_STOPWORDS = frozenset(
    "the and for with that this from what where when which who how does did "
    "are was were has have had not but you your our its into about there "
    "then than them they will would should could can may also just is it of "
    "to in on at be up an as or if do we my me como para con que por una los "
    "las del donde cuando esta este".split()
)

# Monotonic clock indirection so deadline behaviour is testable.
_clock = time.monotonic


@dataclass(frozen=True)
class ExpansionCaps:
    """Hard bounds for one expansion; every value is clamped on load."""

    max_hops: int = 2
    max_seeds: int = 5
    fanout: int = 6
    max_candidates: int = 24
    token_budget: int = 600
    base_share: float = 0.5
    deadline_ms: int = 250

    _ENV = {
        "max_hops": ("MEMORYMASTER_RECALL_GRAPH_EXPAND_HOPS", 1, 2),
        "max_seeds": ("MEMORYMASTER_RECALL_GRAPH_EXPAND_SEEDS", 1, 20),
        "fanout": ("MEMORYMASTER_RECALL_GRAPH_EXPAND_FANOUT", 1, 50),
        "max_candidates": ("MEMORYMASTER_RECALL_GRAPH_EXPAND_MAX_CANDIDATES", 1, 200),
        "token_budget": ("MEMORYMASTER_RECALL_GRAPH_EXPAND_TOKEN_BUDGET", 0, 20000),
        "base_share": ("MEMORYMASTER_RECALL_GRAPH_EXPAND_BASE_SHARE", 0.0, 1.0),
        "deadline_ms": ("MEMORYMASTER_RECALL_GRAPH_EXPAND_DEADLINE_MS", 10, 900),
    }

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ExpansionCaps:
        env = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        for name, (var, low, high) in cls._ENV.items():
            default = getattr(cls, name)
            raw = (env.get(var) or "").strip()
            try:
                number = float(raw) if raw else float(default)
            except ValueError:
                number = float(default)
            if not math.isfinite(number):
                number = float(default)
            value = type(default)(number)
            values[name] = min(max(value, type(default)(low)), type(default)(high))
        return cls(**values)


@dataclass
class ExpansionOutcome:
    mode: str
    applied: bool
    rows: list[dict[str, Any]]
    fallback_reason: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)

    def report(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "applied": self.applied,
            "fallback_reason": self.fallback_reason,
            "stats": dict(self.stats),
        }


def resolve_graph_mode(
    explicit: str | None, environ: Mapping[str, str] | None = None
) -> str:
    """Explicit argument wins; otherwise the env flag; unknown values -> off."""
    env = os.environ if environ is None else environ
    raw = explicit if explicit is not None else env.get(MODE_ENV, "")
    value = (raw or "").strip().lower()
    return value if value in GRAPH_MODES else "off"


class _Timeout(Exception):
    pass


@dataclass(frozen=True)
class _Anchor:
    """Tenant/scope/principal boundary every claim on a path must share."""

    scopes: frozenset[str]
    tenants: frozenset[str | None]
    principals: frozenset[str | None]


@dataclass(frozen=True)
class _Step:
    source: int
    neighbour: int
    kind: str
    weight: float
    supports: tuple[int, ...] = ()


@dataclass(frozen=True)
class _Path:
    seed_id: int | None
    claim_ids: tuple[int, ...]
    kinds: tuple[str, ...]
    supports: tuple[int, ...]
    score: float
    anchor: _Anchor

    @property
    def hops(self) -> int:
        return len(self.kinds)

    def extend(self, step: _Step) -> _Path:
        return _Path(
            seed_id=self.seed_id,
            claim_ids=(*self.claim_ids, step.neighbour),
            kinds=(*self.kinds, step.kind),
            supports=tuple(dict.fromkeys((*self.supports, *step.supports))),
            score=self.score * HOP_DECAY * step.weight,
            anchor=self.anchor,
        )


def is_generated_claim(claim: Any) -> bool:
    """Derived output (observations, skills, summaries) is never evidence."""
    return (
        (getattr(claim, "claim_type", None) or "") in GENERATED_CLAIM_TYPES
        or (getattr(claim, "source_agent", None) or "") in GENERATED_SOURCE_AGENTS
    )


class ClaimAuthorizer:
    """Rehydrate claims through the store and re-check authority in code."""

    def __init__(
        self,
        get_claim: Callable[[int], Any],
        *,
        statuses: Sequence[str],
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self._get_claim = get_claim
        self._statuses = frozenset(statuses)
        self._conn = conn
        self._claims: dict[int, Any] = {}
        self._lineage: dict[int, str | None] = {}
        self._checked: dict[tuple[int, _Anchor, str], str | None] = {}
        self.rejections: Counter[str] = Counter()
        self._observations = conn is not None and _has_table(conn, "graph_observations")
        self._lineage_tables = conn is not None and all(
            _has_table(conn, name)
            for name in ("claim_evidence_links", "evidence_items", "source_items")
        )

    def _is_generated(self, claim: Any) -> bool:
        if is_generated_claim(claim):
            return True
        if not self._observations:
            return False
        return self._conn.execute(
            "SELECT 1 FROM graph_observations WHERE observation_claim_id = ?",
            (int(claim.id),),
        ).fetchone() is not None

    def claim(self, claim_id: int) -> Any:
        if claim_id not in self._claims:
            self._claims[claim_id] = self._get_claim(int(claim_id))
        return self._claims[claim_id]

    def check(self, claim: Any, anchor: _Anchor, *, role: str) -> str | None:
        key = (int(getattr(claim, "id", 0) or 0), anchor, role)
        if claim is not None and key in self._checked:
            return self._checked[key]
        reason = self._reason(claim, anchor, role=role)
        if reason is not None:
            self.rejections[f"{role}:{reason}"] += 1
        if claim is not None:
            self._checked[key] = reason
        return reason

    def _reason(self, claim: Any, anchor: _Anchor, *, role: str) -> str | None:
        if claim is None:
            return "missing"
        status = getattr(claim, "status", "")
        if status in RETIRED_STATUSES:
            return "retired"
        if status != ACTIVE_STATUS or status not in self._statuses:
            return "inactive"
        if not claim_is_temporally_current(claim):
            return "not_current"
        if getattr(claim, "tenant_id", None) not in anchor.tenants:
            return "tenant"
        if claim.scope not in anchor.scopes:
            return "scope"
        visibility = (getattr(claim, "visibility", "public") or "public").strip().lower()
        if visibility == "sensitive" or is_sensitive_claim(claim):
            return "sensitive"
        if visibility != "public" and getattr(claim, "source_agent", None) not in anchor.principals:
            return "principal"
        citation_reason = _citation_reason(getattr(claim, "citations", None) or [])
        if citation_reason is not None:
            return citation_reason
        lineage = self._lineage_reason(int(claim.id))
        if lineage is not None:
            return lineage
        if role == "support" and self._is_generated(claim):
            return "generated_support"
        return None

    def _lineage_reason(self, claim_id: int) -> str | None:
        """Evidence lineage is optional, but present lineage must be active."""
        if not self._lineage_tables:
            return None
        if claim_id not in self._lineage:
            self._lineage[claim_id] = _lineage_reason(self._conn, claim_id)
        return self._lineage[claim_id]


def _citation_reason(citations: Sequence[Any]) -> str | None:
    valid = [c for c in citations if (getattr(c, "source", "") or "").strip()]
    if not valid:
        return "citation_missing"
    for citation in citations:
        text = " ".join(
            str(getattr(citation, key, "") or "") for key in ("source", "locator", "excerpt")
        )
        if scan_text_for_findings(text):
            return "citation_sensitive"
    return None


def _lineage_reason(conn: sqlite3.Connection, claim_id: int) -> str | None:
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN si.retired_at IS NOT NULL THEN 1 ELSE 0 END) AS retired,
                  SUM(CASE WHEN si.retired_at IS NULL AND ei.sensitivity = 'none'
                                AND si.sensitivity = 'none' THEN 1 ELSE 0 END) AS eligible
           FROM claim_evidence_links cel
           JOIN evidence_items ei ON ei.id = cel.evidence_item_id
           JOIN source_items si ON si.id = ei.source_item_id
           WHERE cel.claim_id = ?""",
        (claim_id,),
    ).fetchone()
    total = int(row[0] or 0)
    if total == 0:
        return None
    if int(row[1] or 0):
        return "evidence_retired"
    if int(row[2] or 0) != total:
        return "evidence_sensitive"
    return None


def authorized_support_citations(
    authorizer: ClaimAuthorizer, support_ids: Sequence[int], anchor: _Anchor
) -> list[dict[str, Any]] | None:
    """Return citation refs for supports, or None when any support fails."""
    citations: list[dict[str, Any]] = []
    for support_id in support_ids:
        support = authorizer.claim(int(support_id))
        if authorizer.check(support, anchor, role="support") is not None:
            return None
        for citation in (support.citations or [])[:2]:
            citations.append(
                {
                    "claim_id": support.id,
                    "source": citation.source,
                    "locator": citation.locator,
                }
            )
    return citations


def anchor_for(
    claim: Any,
    *,
    scope_allowlist: Sequence[str] | None,
    tenant_id: str | None,
    requesting_agent: str | None,
) -> _Anchor:
    """Boundary relative to an authorized claim (seed or candidate)."""
    return _Anchor(
        scopes=frozenset(scope_allowlist) if scope_allowlist is not None else frozenset({claim.scope}),
        tenants=frozenset({tenant_id if tenant_id is not None else getattr(claim, "tenant_id", None)}),
        principals=frozenset(
            {requesting_agent if requesting_agent else getattr(claim, "source_agent", None)}
        ),
    )


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _marks(values: Sequence[Any]) -> str:
    return ",".join("?" * len(values))


def _neighbour_steps(
    conn: sqlite3.Connection, claim_id: int, tables: frozenset[str], fanout: int
) -> list[_Step]:
    """Existing edges out of one claim, best first, at most ``fanout``."""
    per_table = max(fanout * 2, fanout + 2)
    steps: list[tuple[int, float, float, _Step]] = []

    def _add(neighbour: int, kind: str, weight: float, active: int, confidence: float,
             supports: tuple[int, ...] = ()) -> None:
        if neighbour != claim_id:
            steps.append(
                (active, weight, confidence,
                 _Step(claim_id, int(neighbour), kind, weight, supports))
            )

    if "claim_links" in tables:
        link_types = list(EXPANSION_LINK_TYPES)
        for row in conn.execute(
            f"""SELECT CASE WHEN l.source_id = ? THEN l.target_id ELSE l.source_id END AS n,
                       l.link_type, c.status = 'confirmed' AS active, c.confidence
                FROM claim_links l
                JOIN claims c ON c.id = CASE WHEN l.source_id = ? THEN l.target_id
                                             ELSE l.source_id END
                WHERE (l.source_id = ? OR l.target_id = ?)
                  AND l.link_type IN ({_marks(link_types)})
                ORDER BY active DESC, c.confidence DESC, n
                LIMIT ?""",
            (claim_id, claim_id, claim_id, claim_id, *link_types, per_table),
        ):
            _add(row[0], f"link:{row[1]}", EDGE_WEIGHTS["link"], row[2], row[3])
    if "claim_edges" in tables:
        kinds = list(EXPANSION_EDGE_KINDS)
        for row in conn.execute(
            f"""SELECT CASE WHEN e.src_claim_id = ? THEN e.dst_claim_id
                            ELSE e.src_claim_id END AS n,
                       e.edge_kind, c.status = 'confirmed' AS active, c.confidence
                FROM claim_edges e
                JOIN claims c ON c.id = CASE WHEN e.src_claim_id = ? THEN e.dst_claim_id
                                             ELSE e.src_claim_id END
                WHERE (e.src_claim_id = ? OR e.dst_claim_id = ?)
                  AND e.edge_kind IN ({_marks(kinds)})
                ORDER BY active DESC, c.confidence DESC, n
                LIMIT ?""",
            (claim_id, claim_id, claim_id, claim_id, *kinds, per_table),
        ):
            _add(row[0], str(row[1]), EDGE_WEIGHTS[str(row[1])], row[2], row[3])
    if {"claim_entity_links", "entity_edge_supports"} <= tables:
        for row in conn.execute(
            """SELECT b.claim_id AS n, es.relation, es.supporting_claim_id,
                      c.status = 'confirmed' AS active, c.confidence
               FROM claim_entity_links a
               JOIN entity_edge_supports es
                 ON es.source_entity_id = a.entity_id OR es.target_entity_id = a.entity_id
               JOIN claim_entity_links b
                 ON b.entity_id = CASE WHEN es.source_entity_id = a.entity_id
                                       THEN es.target_entity_id ELSE es.source_entity_id END
               JOIN claims c ON c.id = b.claim_id
               WHERE a.claim_id = ? AND b.claim_id <> a.claim_id
               ORDER BY active DESC, c.confidence DESC, n, es.supporting_claim_id
               LIMIT ?""",
            (claim_id, per_table),
        ):
            _add(row[0], f"entity:{row[1]}", EDGE_WEIGHTS["entity"], row[3], row[4],
                 (int(row[2]),))
    steps.sort(key=lambda item: (-item[0], -item[1], -float(item[2] or 0.0), item[3].neighbour))
    chosen: list[_Step] = []
    seen: set[tuple[int, tuple[int, ...]]] = set()
    for *_ignored, step in steps:
        key = (step.neighbour, step.supports)
        if key in seen:
            continue
        seen.add(key)
        chosen.append(step)
        if len(chosen) >= fanout:
            break
    return chosen


def _query_entity_ids(conn: sqlite3.Connection, query_text: str, limit: int) -> list[int]:
    """graph_first seeds: case-insensitive alias match on 1-3 word n-grams."""
    if not _has_table(conn, "entity_aliases"):
        return []
    from memorymaster.knowledge.entity_registry import normalize_alias

    tokens = re.findall(r"[\w][\w.+#-]*", query_text.lower())
    grams: list[str] = []
    for size in (3, 2, 1):
        for index in range(len(tokens) - size + 1):
            gram = tokens[index:index + size]
            if size == 1 and (len(gram[0]) < 3 or gram[0] in _STOPWORDS):
                continue
            if gram[0] in _STOPWORDS or gram[-1] in _STOPWORDS:
                continue
            grams.append(normalize_alias(" ".join(gram)))
    keys = list(dict.fromkeys(key for key in grams if key))[:60]
    if not keys:
        return []
    rows = conn.execute(
        f"""SELECT DISTINCT entity_id FROM entity_aliases
            WHERE alias IN ({_marks(keys)}) ORDER BY entity_id LIMIT ?""",
        (*keys, limit),
    ).fetchall()
    return [int(row[0]) for row in rows]


def _entity_claim_ids(conn: sqlite3.Connection, entity_id: int, fanout: int) -> list[int]:
    rows = conn.execute(
        """SELECT cel.claim_id FROM claim_entity_links cel
           JOIN claims c ON c.id = cel.claim_id
           WHERE cel.entity_id = ?
           ORDER BY c.status = 'confirmed' DESC, c.confidence DESC, cel.claim_id
           LIMIT ?""",
        (entity_id, fanout),
    ).fetchall()
    return [int(row[0]) for row in rows]


def _estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text or "") / 4))


def _merge(
    base: list[dict[str, Any]],
    expanded: list[dict[str, Any]],
    *,
    limit: int,
    base_share: float,
    graph_first: bool,
) -> list[dict[str, Any]]:
    base = base[:limit]
    quota = min(len(base), max(1, math.ceil(limit * base_share)))
    head = base[:quota]
    room = limit - len(head)
    if graph_first:
        return head + (expanded[:room] + base[quota:])[:room]
    # Interleave by score without reordering base rows among themselves;
    # a base row wins every tie.
    rest, extra, tail = base[quota:], expanded[:room], []
    b = e = 0
    while len(tail) < room and (b < len(rest) or e < len(extra)):
        take_extra = e < len(extra) and (
            b >= len(rest)
            or float(extra[e].get("score") or 0.0) > float(rest[b].get("score") or 0.0)
        )
        if take_extra:
            tail.append(extra[e])
            e += 1
        else:
            tail.append(rest[b])
            b += 1
    return head + tail


def expand_recall(
    base_rows: list[dict[str, Any]],
    query_text: str,
    *,
    mode: str,
    limit: int,
    db_path: str | None,
    get_claim: Callable[[int], Any],
    annotate: Callable[[Any], Any],
    statuses: Sequence[str],
    scope_allowlist: Sequence[str] | None,
    tenant_id: str | None,
    requesting_agent: str | None,
    caps: ExpansionCaps | None = None,
) -> ExpansionOutcome:
    """Expand authorized recall rows over existing edges; never raises."""
    caps = caps or ExpansionCaps.from_env()
    started = _clock()
    stats: dict[str, Any] = {"caps": asdict(caps)}

    def _fallback(reason: str) -> ExpansionOutcome:
        stats["elapsed_ms"] = round((_clock() - started) * 1000.0, 3)
        return ExpansionOutcome(mode, False, base_rows, reason, stats)

    if mode not in ("vector_first", "graph_first"):
        return _fallback("mode_off")
    if not db_path or str(db_path).startswith(("postgres://", "postgresql://")):
        return _fallback("unsupported_store")
    if limit <= 0:
        return _fallback("no_room")
    try:
        # A reader waiting on a checkpoint must not outlive the deadline.
        conn = connect_ro(str(db_path), query_ms=caps.deadline_ms)
    except sqlite3.Error:
        return _fallback("unsupported_store")
    try:
        return _expand(
            conn, base_rows, query_text, mode=mode, limit=limit, get_claim=get_claim,
            annotate=annotate, statuses=statuses, scope_allowlist=scope_allowlist,
            tenant_id=tenant_id, requesting_agent=requesting_agent, caps=caps,
            started=started, stats=stats, fallback=_fallback,
        )
    except _Timeout:
        return _fallback("timeout")
    except Exception as exc:  # noqa: BLE001 - deterministic fallback boundary
        stats["error_type"] = type(exc).__name__
        logger.warning("graph expansion failed (%s); keeping current recall", type(exc).__name__)
        return _fallback("error")
    finally:
        conn.close()


def _expand(
    conn: sqlite3.Connection,
    base_rows: list[dict[str, Any]],
    query_text: str,
    *,
    mode: str,
    limit: int,
    get_claim: Callable[[int], Any],
    annotate: Callable[[Any], Any],
    statuses: Sequence[str],
    scope_allowlist: Sequence[str] | None,
    tenant_id: str | None,
    requesting_agent: str | None,
    caps: ExpansionCaps,
    started: float,
    stats: dict[str, Any],
    fallback: Callable[[str], ExpansionOutcome],
) -> ExpansionOutcome:
    deadline = started + caps.deadline_ms / 1000.0

    def _check_deadline() -> None:
        if _clock() > deadline:
            raise _Timeout

    authorizer = ClaimAuthorizer(get_claim, statuses=statuses, conn=conn)
    base_ids = {
        int(row["claim"].id) for row in base_rows if hasattr(row.get("claim"), "id")
    }
    tables = frozenset(name for name in _GRAPH_TABLES if _has_table(conn, name))
    frontier: dict[int, _Path] = {}
    if mode == "vector_first":
        for row in base_rows[: caps.max_seeds]:
            _check_deadline()
            claim = row.get("claim")
            if not hasattr(claim, "id"):
                continue
            seed = authorizer.claim(int(claim.id))
            anchor = anchor_for(seed or claim, scope_allowlist=scope_allowlist,
                                tenant_id=tenant_id, requesting_agent=requesting_agent)
            if authorizer.check(seed, anchor, role="support") is not None:
                continue
            base_score = float(row.get("score") or 0.0) or 1e-6
            frontier[seed.id] = _Path(seed.id, (seed.id,), (), (), base_score, anchor)
    else:
        frontier = _graph_first_frontier(
            conn, base_rows, query_text, authorizer, caps=caps,
            scope_allowlist=scope_allowlist, tenant_id=tenant_id,
            requesting_agent=requesting_agent, check_deadline=_check_deadline,
        )
    stats["seeds"] = len(frontier)
    if not frontier:
        stats["rejections"] = dict(authorizer.rejections)
        return fallback("no_seeds")
    if not tables:
        return fallback("no_graph")

    paths: dict[int, _Path] = {}
    if mode == "graph_first":
        paths.update({cid: path for cid, path in frontier.items() if cid not in base_ids})
    steps_seen = 0
    fresh = 0
    for _hop in range(caps.max_hops if mode == "vector_first" else max(caps.max_hops - 1, 0)):
        _check_deadline()
        next_frontier: dict[int, _Path] = {}
        for node_id, path in frontier.items():
            for step in _neighbour_steps(conn, node_id, tables, caps.fanout):
                # Per step, not per node: one slow rehydration must not keep a
                # whole fanout running past the deadline.
                _check_deadline()
                steps_seen += 1
                neighbour = step.neighbour
                if neighbour in base_ids or neighbour in path.claim_ids:
                    continue
                fresh += 1
                candidate_path = path.extend(step)
                known = paths.get(neighbour)
                if known is not None and known.score >= candidate_path.score:
                    continue
                if known is None and len(paths) >= caps.max_candidates:
                    stats["candidate_cap_hits"] = stats.get("candidate_cap_hits", 0) + 1
                    continue
                if not _path_authorized(authorizer, neighbour, candidate_path):
                    continue
                paths[neighbour] = candidate_path
                next_frontier[neighbour] = candidate_path
            _check_deadline()
        frontier = next_frontier
        if not frontier:
            break
    stats["steps_seen"] = steps_seen
    stats["fresh_neighbours"] = fresh
    stats["rejections"] = dict(authorizer.rejections)
    stats["retired_rejections"] = sum(
        count for key, count in authorizer.rejections.items()
        if key.endswith((":retired", ":evidence_retired"))
    )
    if not paths:
        return fallback("no_candidates" if fresh == 0 else "no_valid_supports")

    expanded: list[dict[str, Any]] = []
    tokens = 0
    for claim_id, path in sorted(paths.items(), key=lambda item: (-item[1].score, item[0])):
        _check_deadline()
        claim = authorizer.claim(claim_id)
        cost = _estimate_tokens(claim.text)
        if tokens + cost > caps.token_budget:
            stats["token_cap_hits"] = stats.get("token_cap_hits", 0) + 1
            continue
        citations = authorized_support_citations(
            authorizer, _support_ids(path, claim_id), path.anchor
        )
        if not citations:
            continue
        tokens += cost
        expanded.append(_row(claim, path, mode=mode, citations=citations, annotate=annotate))
    stats["expanded_tokens"] = tokens
    if not expanded:
        return fallback("no_room" if stats.get("token_cap_hits") else "no_valid_supports")
    _check_deadline()
    rows = _merge(base_rows, expanded, limit=limit, base_share=caps.base_share,
                  graph_first=mode == "graph_first")
    added = [row for row in rows if row.get("source") == "graph_expansion"]
    if not added:
        return fallback("no_room")
    _check_deadline()  # late answers never act
    stats["added"] = len(added)
    stats["added_tokens"] = sum(_estimate_tokens(row["claim"].text) for row in added)
    stats["elapsed_ms"] = round((_clock() - started) * 1000.0, 3)
    return ExpansionOutcome(mode, True, rows, None, stats)


def _support_ids(path: _Path, candidate_id: int) -> list[int]:
    """Claims that justify the path: seed, intermediates and edge supports."""
    ids = [cid for cid in path.claim_ids if cid != candidate_id]
    ids.extend(path.supports)
    if not ids:  # graph_first hop 0: the candidate's own entity mention
        ids.append(candidate_id)
    return list(dict.fromkeys(ids))


def _path_authorized(authorizer: ClaimAuthorizer, candidate_id: int, path: _Path) -> bool:
    candidate = authorizer.claim(candidate_id)
    if authorizer.check(candidate, path.anchor, role="candidate") is not None:
        return False
    return authorized_support_citations(
        authorizer, _support_ids(path, candidate_id), path.anchor
    ) is not None


def _graph_first_frontier(
    conn: sqlite3.Connection,
    base_rows: list[dict[str, Any]],
    query_text: str,
    authorizer: ClaimAuthorizer,
    *,
    caps: ExpansionCaps,
    scope_allowlist: Sequence[str] | None,
    tenant_id: str | None,
    requesting_agent: str | None,
    check_deadline: Callable[[], None] = lambda: None,
) -> dict[int, _Path]:
    if "claim_entity_links" not in {n for n in _GRAPH_TABLES if _has_table(conn, n)}:
        return {}
    base_claims = [row["claim"] for row in base_rows if hasattr(row.get("claim"), "scope")]
    scopes = (
        frozenset(scope_allowlist) if scope_allowlist is not None
        else frozenset(claim.scope for claim in base_claims)
    )
    if not scopes:
        return {}
    anchor = _Anchor(
        scopes=scopes,
        tenants=frozenset(
            {tenant_id} if tenant_id is not None
            else {getattr(claim, "tenant_id", None) for claim in base_claims} or {None}
        ),
        principals=frozenset(
            {requesting_agent} if requesting_agent
            else {getattr(claim, "source_agent", None) for claim in base_claims}
        ),
    )
    frontier: dict[int, _Path] = {}
    for entity_id in _query_entity_ids(conn, query_text, caps.max_seeds):
        for claim_id in _entity_claim_ids(conn, entity_id, caps.fanout):
            check_deadline()
            if claim_id in frontier or len(frontier) >= caps.max_candidates:
                continue
            path = _Path(None, (claim_id,), (), (), 1.0, anchor)
            if _path_authorized(authorizer, claim_id, path):
                frontier[claim_id] = path
    return frontier


def _row(
    claim: Any,
    path: _Path,
    *,
    mode: str,
    citations: list[dict[str, Any]],
    annotate: Callable[[Any], Any],
) -> dict[str, Any]:
    explanation = {
        "mode": mode,
        "seed_claim_id": path.seed_id,
        "hops": path.hops,
        "path_claim_ids": list(path.claim_ids),
        "edge_kinds": list(path.kinds),
        "supporting_claim_ids": _support_ids(path, claim.id),
        "citations": citations,
        "path_score": round(path.score, 6),
    }
    return {
        "claim": claim,
        "status": claim.status,
        "annotation": annotate(claim),
        "score": path.score,
        "lexical_score": 0.0,
        "freshness_score": 0.0,
        "confidence_score": claim.confidence,
        "vector_score": 0.0,
        "source": "graph_expansion",
        "breakdown": {"graph_expansion": explanation},
        "graph_explanation": explanation,
    }


# ---------------------------------------------------------------------------
# MemoryService glue (moved from core/service.py without behaviour change)
# ---------------------------------------------------------------------------


def recall_with_graph(
    service: Any,
    results: list[dict[str, Any]],
    query_text: str,
    limit: int,
    *,
    mode: str,
    statuses: Sequence[str],
    scope_allowlist: Sequence[str] | None,
    requesting_agent: str | None,
    visibility_filter: Callable[[list[Any], str | None], list[Any]],
    enrich_with_entities: bool = False,
    allow_sensitive: bool = False,
) -> list[dict[str, Any]]:
    """Post-ranking graph step of ``query_rows``; ``off`` without enrichment is a no-op.

    A graph mode runs the bounded expansion; any fallback keeps the current
    recall, including the current entity enrichment when it was requested.
    """
    if mode != "off":
        outcome = expand_recall(
            results,
            query_text,
            mode=mode,
            limit=limit,
            db_path=str(getattr(service.store, "db_path", "") or "") or None,
            get_claim=lambda cid: service.store.get_claim(cid, include_citations=True),
            annotate=service._annotation_for_claim,
            statuses=statuses,
            scope_allowlist=scope_allowlist,
            tenant_id=service.tenant_id,
            requesting_agent=requesting_agent,
        )
        service.last_graph_expansion = outcome.report()
        if outcome.applied:
            return outcome.rows
    if enrich_with_entities:
        return enrich_with_entity_graph(
            service,
            results,
            query_text,
            limit,
            scope_allowlist=scope_allowlist,
            allow_sensitive=allow_sensitive,
            statuses=statuses,
            requesting_agent=requesting_agent,
            visibility_filter=visibility_filter,
        )
    return results


def enrich_with_entity_graph(
    service: Any,
    results: list[dict[str, Any]],
    query_text: str,
    limit: int,
    *,
    scope_allowlist: Sequence[str] | None = None,
    allow_sensitive: bool = False,
    statuses: Sequence[str] | None = None,
    requesting_agent: str | None = None,
    visibility_filter: Callable[[list[Any], str | None], list[Any]],
) -> list[dict[str, Any]]:
    """Merge governed entity candidates without making graph retrieval default.

    Entity paths are discovery hints only: every graph candidate is
    rehydrated through the authoritative claim store and rechecked against
    the same lifecycle, tenant, scope, sensitivity, and principal rules as
    ranked rows. Valid graph evidence reserves result slots so a full
    lexical top-k cannot turn opt-in enrichment into a vacancy-only path.
    """
    from memorymaster.knowledge.entity_graph import EntityGraph

    query_words = [
        word for word in query_text.split() if len(word) > 3 and word[0].isupper()
    ]
    if not query_words:
        return results
    db_target = str(
        getattr(service.store, "db_path", "") or getattr(service.store, "dsn", "")
    )
    if not db_target:
        return results
    graph = EntityGraph(db_target, read_only=True)
    related = graph.find_related_claims_explained(
        query_words,
        hops=2,
        limit=limit,
        scope_allowlist=scope_allowlist,
    )
    existing_ids = {
        row["claim"].id for row in results if hasattr(row.get("claim"), "id")
    }
    allowed_statuses = set(statuses or ("confirmed",))
    # Supports are evidence shown to the caller (ids + citations), so they
    # are re-authorized like the candidate: tenant, scope, principal,
    # sensitivity, active status and citation.
    support_authorizer = ClaimAuthorizer(
        lambda cid: service.store.get_claim(cid, include_citations=True),
        statuses=("confirmed",),
    )
    graph_rows: list[dict[str, Any]] = []
    for explanation in related:
        claim_id = int(explanation["claim_id"])
        if claim_id in existing_ids:
            continue
        claim = service.store.get_claim(claim_id, include_citations=True)
        if claim is None or claim.status not in allowed_statuses:
            continue
        if not claim_is_temporally_current(claim):
            continue
        if service.tenant_id is not None and claim.tenant_id != service.tenant_id:
            continue
        if scope_allowlist is not None and claim.scope not in scope_allowlist:
            continue
        if not allow_sensitive and is_sensitive_claim(claim):
            continue
        if not visibility_filter([claim], requesting_agent):
            continue
        support_anchor = anchor_for(
            claim,
            scope_allowlist=scope_allowlist,
            tenant_id=service.tenant_id,
            requesting_agent=requesting_agent,
        )
        if authorized_support_citations(
            support_authorizer,
            explanation.get("supporting_claim_ids") or [],
            support_anchor,
        ) is None:
            continue
        graph_rows.append(
            _entity_graph_row(claim, explanation=explanation, annotate=service._annotation_for_claim)
        )
        existing_ids.add(claim.id)

    if not graph_rows:
        return results[:limit]
    graph_rows = graph_rows[:limit]
    lexical_limit = max(0, limit - len(graph_rows))
    return results[:lexical_limit] + graph_rows


def _entity_graph_row(
    claim: Any, *, explanation: dict[str, Any] | None, annotate: Callable[[Any], Any]
) -> dict[str, Any]:
    return {
        "claim": claim,
        "status": claim.status,
        "annotation": annotate(claim),
        "score": 0.3,
        "lexical_score": 0.0,
        "freshness_score": 0.0,
        "confidence_score": claim.confidence,
        "vector_score": 0.0,
        "source": "entity_graph",
        "breakdown": {"entity_graph": explanation or {}},
        "graph_explanation": explanation or {},
    }
