"""Opt-in ``rrf`` retrieval mode: parallel streams -> RRF -> cross-encoder -> budget.

Idea borrowed from vectorize-io/hindsight (MIT, see CREDITS.md): retrieve with
several independent strategies, merge by rank with reciprocal rank fusion, then
let a local cross-encoder reorder only the fused head.

Streams (each one an ordered list of AUTHORIZED claims):

* ``lexical`` - the store's FTS5/BM25 order (``MemoryService._legacy_candidates``).
* ``dense``   - cosine over the authorized candidate pool (``store.vector_scores``).
* ``entity``  - claims linked to entities whose alias appears in the query.
* ``time``    - claims dated inside a time range named in the query.

Vectors, entity links and date filters only PROPOSE ids. The entity and time
streams rehydrate every id from SQLite with the same status / scope / tenant
filter as the other modes and then pass the shared authorizer (temporal,
sensitivity, per-agent visibility), so no stream can widen what recall returns.

Enable with ``MEMORYMASTER_RECALL_MODE=rrf`` (applies when the caller asks for
the default ``legacy`` mode) or pass ``retrieval_mode="rrf"``. Default behaviour
is unchanged. Unmeasured RRF once regressed MAP@5 by 49% here (see
``local_rerank``): measure with ``scripts/bench_recall_vs_raw.py`` before
recommending it.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from itertools import zip_longest
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from memorymaster.core.models import Claim
from memorymaster.recall import local_rerank
from memorymaster.recall.recall_fusion import RRF_K_DEFAULT, rrf_fuse

logger = logging.getLogger(__name__)

RRF_MODE = "rrf"
MODE_ENV = "MEMORYMASTER_RECALL_MODE"
RERANK_ENV = "MEMORYMASTER_RECALL_RRF_RERANK"  # "0" disables the cross-encoder
TOP_N_ENV = "MEMORYMASTER_RECALL_RRF_TOP_N"  # fused head size handed to the reranker
TOKEN_BUDGET_ENV = "MEMORYMASTER_RECALL_RRF_TOKEN_BUDGET"  # unset/0 = limit only
DEFAULT_TOP_N = 30
STREAM_DEPTH = 100

ScoreFn = Callable[[str, "list[str]"], "list[float] | None"]


def resolve_mode(retrieval_mode: str) -> str:
    """Map the default ``legacy`` request to ``rrf`` when the env opts in.

    Callers that pass ``legacy`` explicitly (dreaming, demo) are indistinguishable
    from the default, so the opt-in applies to them too; any other explicit mode
    (``hybrid``, ``qdrant``) is never overridden.
    """
    if retrieval_mode == "legacy" and os.environ.get(MODE_ENV, "").strip().lower() == RRF_MODE:
        return RRF_MODE
    return retrieval_mode


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def rerank_enabled() -> bool:
    return os.environ.get(RERANK_ENV, "1").strip() != "0"


@dataclass(slots=True)
class FusedHit:
    """One fused claim. Exposes ``.claim`` so the session-diversity cap applies."""

    claim: Claim
    rrf_score: float
    ranks: dict[str, int] = field(default_factory=dict)  # stream -> 1-based rank
    vector_score: float = 0.0
    rerank_score: float | None = None


def fuse_streams(streams: Mapping[str, Sequence[Claim]], *, k: int = RRF_K_DEFAULT) -> list[FusedHit]:
    """RRF over ordered claim streams; ties keep the best single-stream rank, then id."""
    by_id: dict[int, Claim] = {}
    ranks: dict[int, dict[str, int]] = {}
    for name, claims in streams.items():
        for position, claim in enumerate(claims, start=1):
            if claim.id in ranks and name in ranks[claim.id]:
                continue  # a stream repeating an id counts once, at its best rank
            by_id.setdefault(claim.id, claim)
            ranks.setdefault(claim.id, {})[name] = position
    scores = rrf_fuse({name: list(dict.fromkeys(c.id for c in claims)) for name, claims in streams.items()}, k=k)
    hits = [FusedHit(by_id[cid], score, ranks[cid]) for cid, score in scores.items()]
    hits.sort(key=lambda h: (-h.rrf_score, min(h.ranks.values()), h.claim.id))
    return hits


def rerank_head(
    query: str, hits: list[FusedHit], *, top_n: int, score_fn: ScoreFn | None = None
) -> list[FusedHit]:
    """Cross-encoder reorder of ``hits[:top_n]``; any failure keeps the fused order."""
    head = hits[:max(0, top_n)]
    if len(head) <= 1 or not query.strip():
        return hits
    captured: list[float] | None = None
    scorer = score_fn if score_fn is not None else (
        lambda q, texts: local_rerank.score_pairs(q, texts)
    )

    def _capture(q: str, texts: list[str]) -> list[float] | None:
        nonlocal captured
        captured = scorer(q, texts)
        return captured

    rows = [{"claim": h.claim, "hit": h} for h in head]
    reordered = local_rerank.rerank_ranked_rows(query, rows, score_fn=_capture)
    if reordered is rows or captured is None or len(captured) != len(head):
        return hits  # scoring failed or was skipped: fused order untouched
    for hit, score in zip(head, captured):
        hit.rerank_score = float(score)
    return [row["hit"] for row in reordered] + hits[len(head):]


def trim_to_budget(hits: list[FusedHit], limit: int, token_budget: int) -> list[FusedHit]:
    """First ``limit`` hits, further cut to ``token_budget`` tokens (>= 1 hit kept)."""
    from memorymaster.recall.context_optimizer import estimate_tokens

    out = hits[:limit]
    if token_budget <= 0:
        return out
    kept: list[FusedHit] = []
    used = 0
    for hit in out:
        cost = estimate_tokens(f"{hit.claim.subject or ''} {hit.claim.text}")
        if kept and used + cost > token_budget:
            break
        kept.append(hit)
        used += cost
    return kept


# --- time range parsing -------------------------------------------------------

_ISO_DAY = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_ISO_MONTH = re.compile(r"\b(\d{4})-(\d{2})\b")
_LAST_N = re.compile(
    r"\b(?:last|past|[uú]ltim[oa]s)\s+(\d{1,3})\s+(day|week|month|d[ií]a|semana|mes)", re.IGNORECASE)
_UNIT_DAYS = {"day": 1, "dia": 1, "día": 1, "week": 7, "semana": 7, "month": 30, "mes": 30}
_RELATIVE = (  # (pattern, days back from today, inclusive span in days)
    (re.compile(r"\b(?:today|hoy)\b", re.IGNORECASE), 0, 1),
    (re.compile(r"\b(?:yesterday|ayer)\b", re.IGNORECASE), 1, 1),
    (re.compile(r"\b(?:last week|la semana pasada)\b", re.IGNORECASE), 7, 7),
    (re.compile(r"\b(?:last month|el mes pasado)\b", re.IGNORECASE), 30, 30),
    (re.compile(r"\b(?:this week|esta semana)\b", re.IGNORECASE), 6, 7),
    (re.compile(r"\b(?:this month|este mes)\b", re.IGNORECASE), 29, 30),
)


def parse_time_range(query: str, today: date | None = None) -> tuple[str, str] | None:
    """Return ``(start, end)`` ISO dates (start inclusive, end exclusive) or ``None``.

    shortcut: rolling windows (``last month`` = 30 days), no calendar-aware
    parsing or locale month names; upgrade if the time stream proves useful.
    """
    today = today or datetime.now(timezone.utc).date()
    match = _ISO_DAY.search(query)
    if match:
        try:
            day = date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            return None
        return day.isoformat(), (day + timedelta(days=1)).isoformat()
    match = _ISO_MONTH.search(query)
    if match:
        year, month = int(match[1]), int(match[2])
        if not 1 <= month <= 12:
            return None
        nxt = date(year + (month == 12), month % 12 + 1, 1)
        return date(year, month, 1).isoformat(), nxt.isoformat()
    match = _LAST_N.search(query)
    if match:
        span = int(match[1]) * _UNIT_DAYS[match[2].lower()]
        return (today - timedelta(days=span)).isoformat(), (today + timedelta(days=1)).isoformat()
    for pattern, back, span in _RELATIVE:
        if pattern.search(query):
            start = today - timedelta(days=back)
            return start.isoformat(), (start + timedelta(days=span)).isoformat()
    return None


# --- stream collection --------------------------------------------------------

def _rehydrate(service, ids: Sequence[int], statuses, scopes, authorize) -> list[Claim]:
    """Authoritative reload of proposed ids in proposal order; same filters as other modes."""
    unique = list(dict.fromkeys(int(i) for i in ids))
    if not unique:
        return []
    claims = service.store.list_claims(
        ids=unique, status_in=list(statuses), include_archived=False, include_citations=True,
        scope_allowlist=scopes, tenant_id=service.tenant_id, limit=len(unique),
    )
    order = {cid: pos for pos, cid in enumerate(unique)}
    claims.sort(key=lambda c: order.get(c.id, len(order)))
    return authorize(claims)


def _sqlite_path(service) -> str | None:
    db_path = str(getattr(service.store, "db_path", "") or "")
    if not db_path or db_path.startswith(("postgres://", "postgresql://")):
        return None  # shortcut: entity/time streams are SQLite-only; Postgres simply lacks them
    return db_path


def entity_stream(service, query_text, statuses, scopes, authorize) -> list[Claim]:
    from memorymaster.recall import graph_expansion
    from memorymaster.stores._storage_shared import connect_ro

    db_path = _sqlite_path(service)
    if db_path is None:
        return []
    conn = connect_ro(db_path, query_ms=500)
    try:
        entity_ids = graph_expansion._query_entity_ids(conn, query_text, 8)
        per_entity = [graph_expansion._entity_claim_ids(conn, eid, 25) for eid in entity_ids]
    finally:
        conn.close()
    # Round-robin so one chatty entity cannot occupy the whole stream.
    ids = [cid for tier in zip_longest(*per_entity) for cid in tier if cid is not None]
    return _rehydrate(service, ids[:STREAM_DEPTH], statuses, scopes, authorize)


def time_stream(service, query_text, statuses, scopes, authorize) -> list[Claim]:
    from memorymaster.stores._storage_shared import connect_ro

    window = parse_time_range(query_text)
    db_path = _sqlite_path(service)
    if window is None or db_path is None:
        return []
    marks = ",".join("?" for _ in statuses)
    conn = connect_ro(db_path, query_ms=500)
    try:
        rows = conn.execute(
            f"""SELECT id FROM claims
                WHERE status IN ({marks})
                  AND substr(COALESCE(event_time, created_at), 1, 10) >= ?
                  AND substr(COALESCE(event_time, created_at), 1, 10) < ?
                ORDER BY confidence DESC, id DESC LIMIT ?""",
            (*statuses, window[0], window[1], STREAM_DEPTH),
        ).fetchall()
    finally:
        conn.close()
    return _rehydrate(service, [int(r[0]) for r in rows], statuses, scopes, authorize)


def dense_stream(service, query_text, pool: list[Claim], vector_hook=None) -> tuple[list[Claim], dict[int, float]]:
    """Cosine order over the authorized pool (the existing hybrid dense retriever)."""
    if not pool or not query_text.strip():
        return [], {}
    if vector_hook is None and hasattr(service.store, "vector_scores"):
        provider = service.embedding_provider
        query_vector = provider.embed(query_text)

        def vector_hook(text, claims):  # noqa: F811 - local default hook
            return service.store.vector_scores(text, claims, provider, query_vector=query_vector)
    if vector_hook is None:
        return [], {}
    scores = dict(vector_hook(query_text, pool) or {})
    ordered = sorted((c for c in pool if scores.get(c.id, 0.0) > 0.0),
                     key=lambda c: (-scores[c.id], c.id))
    return ordered[:STREAM_DEPTH], scores


def collect_streams(
    service, query_text, *, limit, statuses, normalized_scopes, requesting_agent,
    include_sensitive, visibility_filter, vector_hook=None,
) -> tuple[dict[str, list[Claim]], dict[int, float]]:
    """Run the four retrievers (sequentially; each is bounded) and return their ordered pools."""
    from memorymaster.recall.candidate_pool import authorizer, candidate_streams

    lexical, broad = candidate_streams(
        service, query_text, limit=limit, statuses=statuses, normalized_scopes=normalized_scopes,
        requesting_agent=requesting_agent, include_sensitive=include_sensitive,
        use_llm_rerank=False, visibility_filter=visibility_filter,
    )
    authorize = authorizer(statuses, include_sensitive, visibility_filter, requesting_agent)
    pool = list({c.id: c for c in [*broad, *lexical]}.values())
    streams: dict[str, list[Claim]] = {"lexical": lexical[:STREAM_DEPTH]}
    vector_scores: dict[int, float] = {}
    for name, fetch in (
        ("dense", lambda: dense_stream(service, query_text, pool, vector_hook)),
        ("entity", lambda: entity_stream(service, query_text, statuses, normalized_scopes, authorize)),
        ("time", lambda: time_stream(service, query_text, statuses, normalized_scopes, authorize)),
    ):
        try:
            result = fetch()
        except Exception as exc:  # noqa: BLE001 - a failed stream must not fail recall
            logger.warning("rrf recall: %s stream failed (%s); continuing without it", name, type(exc).__name__)
            continue
        if name == "dense":
            streams[name], vector_scores = result
        else:
            streams[name] = result
    return streams, vector_scores


def rrf_rank(
    query_text: str, streams: Mapping[str, Sequence[Claim]], vector_scores: Mapping[int, float], *,
    limit: int, score_fn: ScoreFn | None = None,
    pending_supersession_ids: frozenset[int] | None = None, diversity_cap: int = 0,
) -> list[FusedHit]:
    """Fuse -> rerank head -> demote superseded -> diversity cap -> trim to limit/budget."""
    from memorymaster.recall.retrieval import apply_session_diversity_cap

    hits = fuse_streams(streams)
    for hit in hits:
        hit.vector_score = float(vector_scores.get(hit.claim.id, 0.0))
    if rerank_enabled():
        hits = rerank_head(query_text, hits, top_n=_int_env(TOP_N_ENV, DEFAULT_TOP_N), score_fn=score_fn)
    if pending_supersession_ids:
        hits = [h for h in hits if h.claim.id not in pending_supersession_ids] + [
            h for h in hits if h.claim.id in pending_supersession_ids]
    hits = apply_session_diversity_cap(hits, diversity_cap)  # type: ignore[arg-type]
    return trim_to_budget(hits, limit, _int_env(TOKEN_BUDGET_ENV, 0))


def hit_breakdown(hit: FusedHit) -> dict[str, Any]:
    return {"mode": RRF_MODE, "rrf": hit.rrf_score, "ranks": dict(hit.ranks), "rerank": hit.rerank_score}
