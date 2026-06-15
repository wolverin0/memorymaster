"""Context hook — automatic memory extraction and injection for Claude Code.

Two functions:
  1. recall(query) — query memorymaster for relevant context before responding
  2. observe(text, source) — extract and ingest claims after a conversation turn

Designed to be called from Claude Code hooks or CLAUDE.md instructions.

Usage (CLI):
    memorymaster recall "what is the user working on?"
    memorymaster observe --text "User decided to use PostgreSQL" --source "session"
    memorymaster observe --stdin < conversation_turn.txt --source "session"

Usage (from CLAUDE.md):
    Before responding, run: memorymaster recall "<user message summary>"
    After important decisions: memorymaster observe --text "<decision>" --source "session"
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from memorymaster.core.hook_log import log_hook

logger = logging.getLogger(__name__)

# v3.9.1 S1 — once-per-process flags so import-failure warnings don't spam.
_VERBATIM_IMPORT_WARNED: bool = False
# v3.9.1 S2 — once-per-process flag for missing claim_edges table.
_CLAIM_EDGES_MISSING_WARNED: bool = False


# Retrieval latency instrumentation (roadmap 5.1).
#
# Each retrieval stream inside ``recall()`` is timed with ``time.perf_counter``
# — NOT ``time.monotonic`` — because claim 11848 documents a Windows flake
# where ``monotonic()`` can return non-monotonic values across a clock-sync
# boundary, yielding negative deltas. ``perf_counter`` is the stdlib-
# recommended timer for short intervals on all platforms.
#
# Every per-stream sample is emitted as a ``log_hook("recall", "latency",
# stream=<name>, ms=<float>, ...)`` line so downstream aggregators can compute
# p50/p99/mean without re-running the workload. A consolidated
# ``latency_total`` line is emitted once per call so operators can grep one
# line per call and pull every phase in one shot.
#
# Overhead is observation-only: no branch inside the timer changes retrieval,
# ranking, or output. ``log_hook`` itself swallows every exception, so a full
# log-dir that fails to write cannot break recall().
_LATENCY_EVENT = "latency"
_LATENCY_TOTAL_EVENT = "latency_total"


@contextmanager
def _phase_timer(phase_ms: dict[str, float], name: str):
    """Context manager that records ``(perf_counter end - start) * 1000`` into
    ``phase_ms[name]``.

    Uses ``time.perf_counter`` per claim 11848 (Windows timer flake on
    ``time.monotonic``). Safe to nest; each call writes an independent slot.
    The timer does not swallow exceptions — the caller's try/except is the
    authority for error handling.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        phase_ms[name] = (time.perf_counter() - start) * 1000.0


# RRF auto-gate telemetry (roadmap 11.6).
#
# When MEMORYMASTER_RECALL_FUSION=auto, the hook counts how many candidate
# streams are "populated" (at least one row with a non-zero score on that
# stream) and picks RRF when the count meets
# MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD (default 3), falling back to the
# linear combiner otherwise. A per-query-type override
# MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD_<TYPE> (TYPE = a query_classifier
# type uppercased, e.g. FACT_LOOKUP) takes precedence over the global var
# for queries of that type, so sparse query classes can demand more streams
# before paying for RRF. Claim 11898 documents the rationale: on a
# dense 500-Q LongMemEval set with 2+ populated streams, RRF wins hit@1
# +18% / MRR +11% vs linear; on a 30-prompt conversational set with only
# bm25+freshness populated, RRF regresses p@5 from 0.313 to 0.127. The
# gate is a stream-topology heuristic, not a universal "RRF > linear"
# claim.
#
# Stats shape mirrors ``llm_provider._FALLBACK_STATS`` (claim 11.1) so
# operators can check get_auto_gate_stats() the same way they check
# get_fallback_stats().
_AUTO_GATE_STATS: dict[str, int] = {
    "calls": 0,
    "picked_rrf": 0,
    "picked_linear": 0,
}
_AUTO_GATE_THRESHOLD_DEFAULT = 3


def get_auto_gate_stats() -> dict[str, int]:
    """Return a copy of RRF auto-gate telemetry counters.

    Counters:
        calls          — total times the auto-gate decided a fusion mode
        picked_rrf     — decisions that selected RRF
        picked_linear  — decisions that selected linear combiner

    Only incremented when MEMORYMASTER_RECALL_FUSION=auto. When
    MEMORYMASTER_RECALL_FUSION=linear (default) or =rrf, the gate code
    is never reached, so all counters stay at 0.
    """
    return dict(_AUTO_GATE_STATS)


def reset_auto_gate_stats() -> None:
    """Reset RRF auto-gate telemetry counters to zero (test helper)."""
    for key in _AUTO_GATE_STATS:
        _AUTO_GATE_STATS[key] = 0


def _classify_query_type(query: str | None) -> str | None:
    """Best-effort ``query_classifier`` type for ``query`` (or None).

    Returns one of ``query_classifier.QUERY_TYPES`` (e.g. ``fact_lookup``)
    so a per-type threshold override can be looked up. Defensive: a blank
    query, an import failure, or any classifier error yields None, in which
    case the gate uses the global threshold — it never crashes recall().
    """
    if not query or not query.strip():
        return None
    try:
        from memorymaster.recall.query_classifier import classify_query

        return classify_query(query)
    except Exception as exc:  # noqa: BLE001 — classification is advisory
        logger.debug("auto-gate query classification skipped: %s", exc)
        return None


def _read_threshold_env(env_key: str) -> int | None:
    """Read one threshold env var as a positive int, or None if unusable.

    Returns None when the var is unset/blank, non-integer, or < 1 — the
    caller then falls back to the next source (global var, then the
    constant). Never raises; bad values are logged at WARNING.
    """
    raw = os.environ.get(env_key)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, ignoring (falling back)", env_key, raw)
        return None
    if value < 1:
        logger.warning("%s=%d < 1, ignoring (falling back)", env_key, value)
        return None
    return value


def _auto_gate_threshold(query_type: str | None = None) -> int:
    """Resolve the gate threshold, falling back to 3.

    Resolution order:
      1. ``MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD_<TYPE>`` — per-query-type
         override (``<TYPE>`` is a ``query_classifier`` type uppercased, e.g.
         ``FACT_LOOKUP``). Only consulted when ``query_type`` is provided.
      2. ``MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD`` — global override.
      3. :data:`_AUTO_GATE_THRESHOLD_DEFAULT` (3).

    A blank / non-integer / ``< 1`` value at any level is ignored and the
    next source is consulted, so a typo'd per-type var can't disable the
    global one.
    """
    if query_type:
        per_type = _read_threshold_env(
            f"MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD_{query_type.upper()}"
        )
        if per_type is not None:
            return per_type
    global_threshold = _read_threshold_env("MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD")
    if global_threshold is not None:
        return global_threshold
    return _AUTO_GATE_THRESHOLD_DEFAULT


def _auto_gate_decide(
    rows: list,
    bm25_scores: dict[int, float],
    bm25_on: bool,
    freshness_weight: float,
    threshold: int | None = None,
    query: str | None = None,
) -> tuple[str, int, int]:
    """Decide ``rrf`` vs ``linear`` for MEMORYMASTER_RECALL_FUSION=auto.

    Mutates ``_AUTO_GATE_STATS`` (increments ``calls`` + one of
    ``picked_rrf`` / ``picked_linear``) and emits a ``log_hook("recall",
    "rrf_auto_gate", ...)`` line. Returns ``(decision, populated, threshold)``.

    ``threshold`` defaults to the env-resolved value (see
    ``_auto_gate_threshold``). Passing an explicit integer bypasses env
    lookup entirely — useful for tests. When ``threshold`` is None and
    ``query`` is given, the query is classified via ``classify_query`` so a
    per-type ``MEMORYMASTER_RECALL_AUTO_GATE_THRESHOLD_<TYPE>`` override can
    apply, falling back to the global threshold.
    """
    if threshold is None:
        threshold = _auto_gate_threshold(_classify_query_type(query))
    populated = _count_populated_streams(
        rows,
        bm25_scores,
        bm25_on,
        freshness_weight,
    )
    _AUTO_GATE_STATS["calls"] += 1
    if populated >= threshold:
        decision = "rrf"
        _AUTO_GATE_STATS["picked_rrf"] += 1
    else:
        decision = "linear"
        _AUTO_GATE_STATS["picked_linear"] += 1
    log_hook(
        "recall",
        "rrf_auto_gate",
        decision=decision,
        populated_streams=populated,
        threshold=threshold,
    )
    logger.info(
        "rrf_auto_gate decision=%s populated_streams=%d threshold=%d",
        decision,
        populated,
        threshold,
    )
    return decision, populated, threshold


def _count_populated_streams(
    rows: list,
    bm25_scores: dict[int, float],
    bm25_on: bool,
    freshness_weight: float,
) -> int:
    """Count candidate streams that have at least one non-zero row.

    A stream is "populated" iff at least one row in the candidate pool has
    a non-zero score on that stream. Freshness is only counted when
    ``W_FRESHNESS > 0`` — otherwise the stream is present in the row dict
    but W_FRESHNESS=0.0 means it contributes nothing to the linear combiner,
    so it should not nudge the gate toward RRF either.

    Streams counted: bm25, entity, vector, verbatim, freshness (gated on
    W_FRESHNESS), graph. Returns an integer in [0, 6].
    """
    count = 0

    # bm25 — only count when the BM25 rescorer is ON. When bm25_on=False,
    # bm25_scores will be empty (or stale), so treat the stream as absent.
    if bm25_on and any(v > 0.0 for v in bm25_scores.values()):
        count += 1

    def _any_positive(field: str) -> bool:
        for r in rows:
            try:
                if float(r.get(field) or 0.0) > 0.0:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    if _any_positive("entity_score"):
        count += 1
    if _any_positive("vector_score"):
        count += 1
    if _any_positive("verbatim_score"):
        count += 1
    if freshness_weight > 0.0 and _any_positive("freshness_score"):
        count += 1
    # graph stream (roadmap 11.3). Only counted when at least one row got
    # a non-zero graph_score — i.e. the stream actually fired and reached
    # the row's claim_id. When MEMORYMASTER_RECALL_GRAPH=0 (default) the
    # field is absent / 0.0 on every row, so this branch is a no-op and
    # the populated count stays bit-identical with the 5-stream baseline.
    if _any_positive("graph_score"):
        count += 1

    return count


# BM25 lexical re-scorer (ships on by default after the 5x5 k1/b sweep
# on 30-prompt eval — see artifacts/bm25-sweep-2026-04-23.md).
#
# Beats the previous overlap-based `_lexical_score` by +0.113 p@5 and
# +0.108 MAP@5 on the 30-prompt eval with non-empty rate held at 28/30.
# k1=1.2, b=0.25 are the shipped defaults (tied with six other combos
# at p@5=0.393; picked because they are classical-BM25 canonical values
# and maximise MAP@5 across ties). Override via env:
#     MEMORYMASTER_BM25_K1=<float>
#     MEMORYMASTER_BM25_B=<float>
#     MEMORYMASTER_LEXICAL_BM25=0           # disable, fall back to overlap scorer
_BM25_K1_DEFAULT = 1.2
_BM25_B_DEFAULT = 0.25

# Per-field BM25 weights (roadmap 1.4). Subject + text are scored
# independently and combined. Defaults are a neutral (1.0, 1.0) because the
# 2026-04-23 eval on 30 real prompts produced a NULL RESULT for every
# non-neutral weighting — subject-heavy (2.0 / 3.0 / 5.0 / 10.0) all
# regressed p@5 vs the concat baseline, and text-heavy (0.0 / 10.0) tied
# MAP but lost p@5. Keep the code path live (we get the infrastructure and
# the per-field headroom for future tuning) but don't promote any config
# that didn't clear the +0.02 p@5 bar. See
# artifacts/bm25-per-field-eval-2026-04-23.md for the full table.
_BM25_W_SUBJECT_DEFAULT = 1.0
_BM25_W_TEXT_DEFAULT = 1.0


def _bm25_param(name: str, default: float) -> float:
    raw = os.environ.get(f"MEMORYMASTER_BM25_{name}")
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid MEMORYMASTER_BM25_%s=%r, falling back to %.2f",
                       name, raw, default)
        return default


def _bm25_field_weight(name: str, default: float) -> float:
    """Read a per-field BM25 weight from env (e.g. ``W_SUBJECT``)."""
    env_key = f"MEMORYMASTER_BM25_{name}"
    raw = os.environ.get(env_key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, falling back to %.2f",
                       env_key, raw, default)
        return default


def _bm25_enabled() -> bool:
    raw = os.environ.get("MEMORYMASTER_LEXICAL_BM25", "1").strip()
    return raw not in ("0", "false", "False", "no", "off", "")


# Recall re-ranker weights (8 dims, matches scripts/eval_recall_precision_at_5.py).
# Baseline (w0) held after autoresearch candidate #4 grid search on
# artifacts/real-prompts.jsonl (30 prompts) — grid winner (+0.02 p@5 at
# hook-matched top_k=8) also regressed MAP@5 by -0.006, so baseline wins.
# Override any single weight via env var, e.g.:
#     MEMORYMASTER_RECALL_W_FRESHNESS=0.15
#     MEMORYMASTER_RECALL_W_ENTITY=0.15
# See artifacts/eval/recall-precision-grid-k8-mov1.jsonl for the full grid.
#
# W_ENTITY (dim 8) powers the entity-link fanout stage. When set to 0.0
# (default), the fanout only runs as a rescue path — i.e. when the FTS5
# stage returned zero hits. Whenever FTS5 produced >=1 hit, the fanout is
# skipped entirely, so the top-K ranking is bit-identical to pre-fanout
# behaviour. Set W_ENTITY > 0 to also run fanout after a non-empty FTS5
# stage and let entity-matched claims compete in the ranker.
_RECALL_WEIGHT_DEFAULTS: dict[str, float] = {
    "W_MATCHES": 0.3,
    "W_PHRASE": 0.3,
    "W_ALL": 0.2,
    # Bumped from 0.1 → 0.3 on 2026-04-23 after the BM25 rescorer (commit 159eef7)
    # replaced the weak overlap scorer. The old W_LEXICAL=0.1 under-weighted the
    # now-much-stronger BM25 signal (see claim 11857). 0.3 matches the observed
    # BM25 agent's isolated measurement of +0.113 p@5 lift when lexical was
    # free to dominate ranking. Override via MEMORYMASTER_RECALL_W_LEXICAL.
    "W_LEXICAL": 0.3,
    "W_CONFIDENCE": 0.1,
    "W_FRESHNESS": 0.0,
    "W_VECTOR": 0.0,
    "W_ENTITY": 0.0,
    # W_VERBATIM — MemPalace-style raw-conversation stream. Off by default
    # (see memorymaster.recall.verbatim_recall). Only contributes when the stream
    # itself is gated on via MEMORYMASTER_RECALL_VERBATIM=1.
    "W_VERBATIM": 0.0,
    # W_GRAPH — Kuzu-backed graph-traversal stream (roadmap 11.3). Off by
    # default; only contributes when MEMORYMASTER_RECALL_GRAPH=1 AND the
    # on-disk Kuzu DB is available (see memorymaster.recall.graph_store). When
    # disabled, graph_score stays at 0.0 on every row so ranking is
    # bit-identical to the 5-stream baseline.
    "W_GRAPH": 0.0,
    # W_CLAIM_TYPE (v3.9.0 F1, ported from MemPalace "Halls" content-type
    # routing). When > 0, classify the query via classify_observation() and
    # boost rows whose claim_type matches the query's intent type by
    # (1.0 + W_CLAIM_TYPE). Default 0.0 keeps ranking bit-identical for
    # legacy callers — the boost is opt-in until the eval validates a default.
    "W_CLAIM_TYPE": 0.0,
    # W_TWO_PASS (v3.9.0 F5, ported from gbrain v0.21 "Cathedral II" two-pass
    # retrieval). When the two-pass stream is ON (MEMORYMASTER_RECALL_TWO_PASS=1)
    # and a claim is reached as a NEIGHBOR of an already-recalled seed claim
    # via shared entities in entity_aliases, ``two_pass_score = 1/(1+hops)``
    # contributes to the linear combiner with this weight. Default 0.0 keeps
    # legacy ranking bit-identical even when the stream is on (rows are still
    # added as candidates but contribute zero to scoring).
    "W_TWO_PASS": 0.0,
    # W_CLOSETS (v3.10 W1, ported from MemPalace v3.3.0). When the closets
    # stream is ON (MEMORYMASTER_RECALL_CLOSETS=1), each claim_id surfaced
    # via ``search_closets`` gets ``closet_score = 1.0`` (constant — closets
    # are a presence boost, not a ranked score). Default 0.0 keeps ranking
    # bit-identical even when the stream is on. MemPalace measured R@1
    # 0.42 → 0.58 (+38%) with regex-derived closets.
    "W_CLOSETS": 0.0,
}


# Graph retrieval stream (roadmap 11.3). Three env vars control the stream:
#
# * ``MEMORYMASTER_RECALL_GRAPH`` — "1" / truthy enables the stream.
#   Default "0" = off, and the entire stream is short-circuited before any
#   Kuzu import so the disabled path has zero overhead.
# * ``MEMORYMASTER_RECALL_GRAPH_MAX_HOPS`` — BFS depth on the
#   claim↔entity bipartite graph. Default 2 (matches Cognee example in
#   artifacts/cognee-assessment-2026-04-24.md).
# * ``MEMORYMASTER_RECALL_GRAPH_PATH`` — Kuzu DB file path. Default
#   ``~/.memorymaster/graph.kuzu``. The backfill script writes here.
# * ``MEMORYMASTER_RECALL_GRAPH_CANDIDATES`` — "1" / truthy promotes the
#   stream from annotation-only to a full HARVEST stream: BFS-reached
#   claims not already in the candidate pool are hydrated as new rows
#   (roadmap 12.2). Default "0" = annotation-only, row set bit-identical.
# * ``MEMORYMASTER_RECALL_W_GRAPH`` — linear-combiner weight for the
#   distance-weighted ``graph_score`` signal (read via ``_recall_weight``).
#   Default 0.0, so even a harvested row contributes nothing to ranking and
#   recall stays bit-identical until an operator opts in to BOTH flags.
#
# Defensive-fail contract (claim 11907): every error is swallowed. If the
# Kuzu DB is missing, corrupt, or kuzu isn't installed, the stream returns
# an empty set of graph-reached claim_ids and recall() falls back to the
# 5-stream stack bit-for-bit.
_GRAPH_MAX_HOPS_DEFAULT = 2
_GRAPH_PATH_DEFAULT = "~/.memorymaster/graph.kuzu"


def _graph_enabled() -> bool:
    raw = os.environ.get("MEMORYMASTER_RECALL_GRAPH", "0").strip()
    return raw not in ("0", "false", "False", "no", "off", "")


def _graph_candidates_enabled() -> bool:
    """Opt-in gate for the graph HARVEST path (roadmap 12.2).

    When ``MEMORYMASTER_RECALL_GRAPH_CANDIDATES`` is truthy AND the graph
    stream itself is enabled (:func:`_graph_enabled`), the graph stream
    promotes from annotation-only to a full retrieval stream: claims reached
    by BFS that are NOT already in the candidate pool are hydrated as new
    rows (distance-weighted ``graph_score``). Default "0" = off, so the
    stream stays annotation-only and recall output is bit-identical to the
    pre-harvest baseline.
    """
    raw = os.environ.get("MEMORYMASTER_RECALL_GRAPH_CANDIDATES", "0").strip()
    return raw not in ("0", "false", "False", "no", "off", "")


def _graph_max_hops() -> int:
    raw = os.environ.get("MEMORYMASTER_RECALL_GRAPH_MAX_HOPS")
    if raw is None or raw.strip() == "":
        return _GRAPH_MAX_HOPS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid MEMORYMASTER_RECALL_GRAPH_MAX_HOPS=%r, falling back to %d",
            raw, _GRAPH_MAX_HOPS_DEFAULT,
        )
        return _GRAPH_MAX_HOPS_DEFAULT
    return max(1, value)


def _graph_path() -> Path:
    raw = os.environ.get("MEMORYMASTER_RECALL_GRAPH_PATH") or _GRAPH_PATH_DEFAULT
    return Path(os.path.expanduser(raw))


def _two_pass_enabled() -> bool:
    """v3.9.0 F5 — opt-in two-pass entity-fanout retrieval gate."""
    raw = os.environ.get("MEMORYMASTER_RECALL_TWO_PASS", "0").strip()
    return raw not in ("0", "false", "False", "no", "off", "")


def _two_pass_use_edges() -> bool:
    """v3.10 W2 — when the two-pass stream is on, ALSO walk claim_edges in addition to entity_aliases."""
    raw = os.environ.get("MEMORYMASTER_RECALL_TWO_PASS_USE_EDGES", "0").strip()
    return raw not in ("0", "false", "False", "no", "off", "")


def _closets_enabled() -> bool:
    """v3.10 W1 — opt-in closets stream gate."""
    raw = os.environ.get("MEMORYMASTER_RECALL_CLOSETS", "0").strip()
    return raw not in ("0", "false", "False", "no", "off", "")


def _closets_boost_only() -> bool:
    """v3.11 P1b — when set, closets only BOOST already-recalled rows.

    The default is OFF (legacy v3.10 behaviour: hydrate new candidates AND
    boost). Boost-only mode is MemPalace's stricter "boost signal, never
    gate" interpretation — it can't displace real lexical matches because
    it never adds new candidates, only re-ranks the existing set.
    """
    raw = os.environ.get("MEMORYMASTER_RECALL_CLOSETS_BOOST_ONLY", "0").strip()
    return raw not in ("0", "false", "False", "no", "off", "")


def _two_pass_max_neighbors() -> int:
    """Cap neighbors discovered per recall call (default 20)."""
    raw = os.environ.get("MEMORYMASTER_RECALL_TWO_PASS_MAX", "20").strip()
    try:
        value = int(raw)
    except ValueError:
        return 20
    return max(1, value)


def _two_pass_neighbor_ids(
    store, seed_ids: list[int], excluded: set[int]
) -> list[int]:
    """Return claim IDs that share entities with any seed claim, capped.

    Walks the entity_aliases / entities tables via the store's underlying
    SQLite connection. Defensive: any DB error returns ``[]``. Excludes IDs
    already seen so two-pass doesn't reintroduce seeds as their own
    neighbors.
    """
    if not seed_ids:
        return []
    cap = _two_pass_max_neighbors()
    placeholder_seeds = ",".join("?" for _ in seed_ids)
    out: list[int] = []
    seen: set[int] = set(excluded)
    try:
        conn = getattr(store, "_conn", None) or getattr(store, "conn", None)
        if conn is None:
            return []
        # 1. entity_ids referenced by any seed claim's text via the
        #    claim_entity_mentions junction (if it exists). Defensive: try
        #    several plausible table/column names so callers with older
        #    schemas still work.
        cursor = conn.execute(
            f"""
            SELECT DISTINCT entity_id FROM claim_entities
            WHERE claim_id IN ({placeholder_seeds})
            """,
            seed_ids,
        )
        entity_ids = [int(r[0]) for r in cursor.fetchall()]
        if not entity_ids:
            return []
        # 2. neighbor claim_ids that mention any of those entities
        placeholder_ents = ",".join("?" for _ in entity_ids)
        cursor = conn.execute(
            f"""
            SELECT DISTINCT claim_id FROM claim_entities
            WHERE entity_id IN ({placeholder_ents})
            LIMIT ?
            """,
            (*entity_ids, cap * 4),  # over-fetch then dedup
        )
        for (cid,) in cursor.fetchall():
            cid = int(cid)
            if cid in seen:
                continue
            seen.add(cid)
            out.append(cid)
            if len(out) >= cap:
                break
    except Exception as exc:  # noqa: BLE001
        logger.debug("two_pass DB walk skipped: %s", exc)
        return []
    return out


def _graph_reached_claim_ids(query: str, store) -> set[int]:
    """Run the graph-traversal stream for ``query`` and return a set of
    claim_ids reachable within ``MAX_HOPS`` hops of the query's entities.

    Defensive: any failure (kuzu missing, DB corrupt, SQLite alias lookup
    broken, network hiccup) returns an empty set so the recall hook is
    unaffected. We only import :mod:`memorymaster.recall.graph_store` when the
    feature flag is on so disabled callers pay zero import cost.

    Kept for backward compatibility (existing tests + future callers
    that want a boolean reach mask). The distance-aware path used by
    the recall ranker is :func:`_graph_reached_claim_distance`.
    """
    if not _graph_enabled():
        return set()
    distance_map = _graph_reached_claim_distance(query, store)
    return set(distance_map.keys())


def _graph_reached_claim_distance(query: str, store) -> dict[int, int]:
    """Distance-weighted variant — returns ``{claim_id: min_hops}``.

    ``min_hops`` is the shortest BFS distance from any of the query's
    entities to the entity the claim mentions. ``hops == 0`` means the
    claim mentions a query entity directly. Callers translate hops into
    a distance-decayed ``graph_score = 1.0 / (1 + hops)`` so closer
    claims rank higher than far ones (roadmap 12.1 — un-cap the
    constant-bonus pathology of the boolean stream).

    Returns an empty dict on any failure path (claim 11907 silent-fail).
    """
    if not _graph_enabled():
        return {}

    # Lazy imports — gated behind the enabled check above.
    try:
        from memorymaster.knowledge.entity_extractor import extract_patterns
        from memorymaster.knowledge.entity_registry import normalize_alias
        from memorymaster.recall.graph_store import GraphStoreUnavailable, open_graph_store
    except Exception as exc:
        logger.debug("graph stream: imports failed: %s", exc)
        return {}

    # Step 1: extract entities from the query and resolve to entity_ids.
    entities = extract_patterns(query or "")
    if not entities:
        return {}

    aliases: list[str] = []
    seen: set[str] = set()
    for ent in entities:
        key = normalize_alias(ent.canonical_hint)
        if not key or key in seen:
            continue
        seen.add(key)
        aliases.append(key)
    if not aliases:
        return {}

    entity_ids: list[int] = []
    try:
        with store.connect() as conn:
            # Alias→entity_id lookup via the same index the entity fanout
            # uses. One SELECT per alias is fine — low-K fanout.
            placeholders = ",".join("?" * len(aliases))
            rows = conn.execute(
                f"SELECT DISTINCT entity_id FROM entity_aliases "
                f"WHERE alias IN ({placeholders})",
                aliases,
            ).fetchall()
            entity_ids = [int(r[0]) for r in rows if r and r[0]]
    except Exception as exc:
        logger.debug("graph stream: alias resolution failed: %s", exc)
        return {}

    if not entity_ids:
        return {}

    # Step 2: traverse the Kuzu graph, collect reachable claims keyed by
    # their min hop distance. The store helper handles BFS + min-hop
    # dedupe in one trip; we just translate failures to {}.
    max_hops = _graph_max_hops()
    gs = None
    try:
        gs = open_graph_store(_graph_path(), allow_networkx=False)
        pairs = gs.claims_for_entities_with_distance(
            entity_ids, max_hops=max_hops, limit=50
        )
        return {int(cid): int(hop) for cid, hop in pairs}
    except GraphStoreUnavailable as exc:
        logger.debug("graph stream: store unavailable: %s", exc)
        return {}
    except Exception as exc:
        logger.debug("graph stream: traversal failed: %s", exc)
        return {}
    finally:
        if gs is not None:
            try:
                gs.close()
            except Exception:  # pragma: no cover - defensive
                pass


def _row_for_graph_claim(claim, graph_score: float) -> dict:
    """Build a query_rows-shaped row for a graph-harvested claim.

    All lexical/vector/entity signals default to zero so the row only
    contributes via ``graph_score`` under ``W_GRAPH``. At ``W_GRAPH=0``
    (shipped default) a harvested row adds nothing to the ranking — it can
    only ever be trimmed by the budget loop, never displace a real hit,
    which preserves the bit-identical-when-disabled guarantee.
    """
    return {
        "claim": claim,
        "status": getattr(claim, "status", "confirmed"),
        "annotation": None,
        "score": 0.0,
        "lexical_score": 0.0,
        "freshness_score": 0.0,
        "confidence_score": float(getattr(claim, "confidence", 0.0) or 0.0),
        "vector_score": 0.0,
        "entity_score": 0.0,
        "graph_score": float(graph_score),
        "source": "graph_harvest",
    }


def _harvest_graph_rows(
    svc,
    graph_distance: dict[int, int],
    rows: list,
    seen_ids: set[int],
) -> int:
    """Hydrate graph-reached claims that are NOT already candidates.

    For each ``(claim_id, hops)`` in ``graph_distance`` whose id is unseen,
    hydrate the claim, skip archived / sensitive ones, and append a row with
    ``graph_score = 1 / (1 + hops)``. Mutates ``rows`` + ``seen_ids`` and
    returns the number of rows added.

    Defensive: a per-claim hydrate error is logged and skipped — the harvest
    never raises into the recall hot path (claim 11907 silent-fail).
    """
    try:
        from memorymaster.core.security import is_sensitive_claim
    except Exception:  # pragma: no cover - security module is core
        is_sensitive_claim = lambda _claim: False  # type: ignore[assignment]  # noqa: E731

    added = 0
    # Closest-first so a future cap keeps the highest-scoring claims.
    for cid, hops in sorted(graph_distance.items(), key=lambda kv: kv[1]):
        if cid in seen_ids:
            continue
        try:
            claim = svc.store.get_claim(cid, include_citations=True)
        except Exception as exc:  # noqa: BLE001 — best-effort hydrate
            logger.debug("graph harvest: get_claim(%d) failed: %s", cid, exc)
            continue
        if claim is None or getattr(claim, "status", "") == "archived":
            continue
        if is_sensitive_claim(claim):
            continue
        rows.append(_row_for_graph_claim(claim, 1.0 / (1.0 + float(hops))))
        seen_ids.add(cid)
        added += 1
    return added


def _recall_weight(name: str) -> float:
    """Read a single recall-ranker weight from env, falling back to default."""
    env_key = f"MEMORYMASTER_RECALL_{name}"
    raw = os.environ.get(env_key)
    if raw is None or raw.strip() == "":
        return _RECALL_WEIGHT_DEFAULTS[name]
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, falling back to default %.2f",
                       env_key, raw, _RECALL_WEIGHT_DEFAULTS[name])
        return _RECALL_WEIGHT_DEFAULTS[name]


# Scope-aware retrieval boost (roadmap 1.2).
#
# When ``MEMORYMASTER_RECALL_SCOPE_BOOST`` > 0, claims whose ``scope`` matches
# the "current project scope" get their ``_relevance`` multiplied by
# ``(1.0 + SCOPE_BOOST)``. Default is 0.0 so the flag is fully opt-in and the
# ranking is bit-identical to pre-boost behaviour when unset.
#
# The current scope is resolved from ``MEMORYMASTER_SCOPE_DEFAULT`` (if set),
# falling back to ``project:memorymaster`` so the typical deployment gets a
# sensible default without forcing every caller to wire env plumbing.
_DEFAULT_CURRENT_SCOPE = "project:memorymaster"


def _recall_scope_boost() -> float:
    """Read ``MEMORYMASTER_RECALL_SCOPE_BOOST`` as float, default 0.0 (off).

    Negative or garbage values fall back to 0.0 (= no boost).
    """
    raw = os.environ.get("MEMORYMASTER_RECALL_SCOPE_BOOST")
    if raw is None or raw.strip() == "":
        return 0.0
    try:
        val = float(raw)
    except ValueError:
        logger.warning(
            "Invalid MEMORYMASTER_RECALL_SCOPE_BOOST=%r, falling back to 0.0", raw
        )
        return 0.0
    # Guard against pathological negatives — they would demote current-scope
    # claims, which is the opposite of the feature's intent. Treat as off.
    return max(0.0, val)


def _current_scope() -> str:
    """Return the "current project scope" used by the scope-boost multiplier.

    Reads ``MEMORYMASTER_SCOPE_DEFAULT`` first; falls back to
    :data:`_DEFAULT_CURRENT_SCOPE` when unset or empty.
    """
    raw = os.environ.get("MEMORYMASTER_SCOPE_DEFAULT")
    if raw is None or raw.strip() == "":
        return _DEFAULT_CURRENT_SCOPE
    return raw.strip()


# Query expansion via entity-matched synonyms (roadmap 1.5).
#
# Env gate: ``MEMORYMASTER_RECALL_QUERY_EXPANSION`` — truthy values enable
# expansion, default (unset / "0") keeps legacy behaviour bit-for-bit.
def _query_expansion_enabled() -> bool:
    raw = os.environ.get("MEMORYMASTER_RECALL_QUERY_EXPANSION", "0").strip()
    return raw not in ("0", "false", "False", "no", "off", "")


def _wal_discipline_enabled() -> bool:
    """P1 WAL-discipline umbrella flag (spec §2.2), default OFF.

    When on, the per-prompt recall hook's MemoryService opens the DB
    strictly read-only (mode=ro + query_only — it can never take a write
    lock on the shared multi-GB file) and its access/feedback records are
    spooled for the steward drain instead of UPDATEd inline. Flag off =
    the untouched legacy RW path.
    """
    from memorymaster.core.spool import wal_discipline_enabled

    return wal_discipline_enabled()


def _apply_query_expansion(svc, query: str, token_list: list[str]) -> list[str]:
    """Augment ``token_list`` with entity-alias tokens from the raw prompt.

    Runs :func:`memorymaster.recall.query_expansion.expand_query` against the
    service's SQLite store; the expansion's ``[query, *aliases]`` output is
    folded into ``token_list`` as additional OR terms for the per-token
    FTS5 fanout. The original query itself is dropped from the expansion
    payload (we only want the alias tokens — the full query is already
    represented by ``token_list``).

    Best-effort: any DB, import, or attribute error returns the unchanged
    ``token_list`` so the feature can never break recall. Deduplication is
    case-insensitive against the existing tokens.
    """
    try:
        from memorymaster.recall.query_expansion import expand_query
    except Exception as exc:  # pragma: no cover — import error unlikely
        logger.debug("query_expansion: import skipped: %s", exc)
        return token_list

    try:
        store = getattr(svc, "store", None)
        conn_ctx = getattr(store, "connect", None) if store is not None else None
        if conn_ctx is None:
            return token_list
        with conn_ctx() as conn:
            expanded = expand_query(query, conn)
    except Exception as exc:
        logger.debug("query_expansion: expand_query failed: %s", exc)
        return token_list

    # expand_query returns [query, *aliases]; drop query since token_list
    # already captures it via extract_query_tokens.
    aliases = expanded[1:] if len(expanded) > 1 else []
    if not aliases:
        return token_list

    lowered_existing = {t.lower() for t in token_list}
    out = list(token_list)
    for alias in aliases:
        key = alias.lower().strip()
        if not key or key in lowered_existing:
            continue
        lowered_existing.add(key)
        out.append(key)
    return out


# Per-call fanout caps. Kept conservative so a pathological prompt (10+ env-vars)
# doesn't blow up the hook budget: at most _ENTITY_CAP_PER_ENTITY claims per
# matched entity, at most _ENTITY_CAP_TOTAL new claims added overall.
_ENTITY_CAP_PER_ENTITY = 3
_ENTITY_CAP_TOTAL = 8


def _entity_fanout_claim_ids(
    store,
    prompt: str,
    seen_ids: set[int],
) -> list[int]:
    """Mine entities from the prompt, resolve to entity_ids via entity_aliases,
    and return claim IDs where ``claims.entity_id`` matches — excluding IDs
    already seen by the FTS5 stage.

    Best-effort: any DB error returns an empty list so the fanout never
    breaks the recall hook. The tables ``entities`` / ``entity_aliases`` are
    created lazily by ``ensure_entity_schema`` at ingest time, so we tolerate
    their absence on legacy DBs.
    """
    try:
        from memorymaster.knowledge.entity_extractor import extract_patterns
        from memorymaster.knowledge.entity_registry import normalize_alias
    except Exception:  # pragma: no cover — import errors are fatal elsewhere
        return []

    entities = extract_patterns(prompt or "")
    if not entities:
        return []

    # Dedupe by normalized alias (entity_extractor already dedupes by
    # canonical_hint, but different kinds can collapse to the same alias form
    # — e.g. "git" as tool vs "git" substring of something else).
    aliases: list[str] = []
    seen_aliases: set[str] = set()
    for ent in entities:
        alias = normalize_alias(ent.canonical_hint)
        if not alias or alias in seen_aliases:
            continue
        seen_aliases.add(alias)
        aliases.append(alias)

    if not aliases:
        return []

    new_ids: list[int] = []
    try:
        with store.connect() as conn:
            # One SELECT per alias so per-entity cap is enforceable
            # without a correlated subquery. Aliases are indexed, so this
            # is cheap even with 10 entities.
            for alias in aliases:
                if len(new_ids) >= _ENTITY_CAP_TOTAL:
                    break
                rows = conn.execute(
                    """
                    SELECT DISTINCT c.id
                      FROM entity_aliases a
                      JOIN claims c ON c.entity_id = a.entity_id
                     WHERE a.alias = ?
                       AND c.status != 'archived'
                       AND (c.visibility IS NULL OR c.visibility = 'public')
                     ORDER BY c.updated_at DESC
                     LIMIT ?
                    """,
                    (alias, _ENTITY_CAP_PER_ENTITY),
                ).fetchall()
                for row in rows:
                    cid = int(row[0])
                    if cid in seen_ids:
                        continue
                    seen_ids.add(cid)
                    new_ids.append(cid)
                    if len(new_ids) >= _ENTITY_CAP_TOTAL:
                        break
    except Exception as exc:
        logger.debug("entity fanout skipped: %s", exc)
        return []

    return new_ids


def _row_for_claim(claim) -> dict:
    """Build a query_rows-shaped row dict for a fanout-sourced claim.

    Scores default to zero so the claim adds no baseline signal; the
    W_ENTITY weight on the ``entity_score`` bit is what promotes it.
    """
    return {
        "claim": claim,
        "status": getattr(claim, "status", "confirmed"),
        "annotation": None,
        "score": 0.0,
        "lexical_score": 0.0,
        "freshness_score": 0.0,
        "confidence_score": float(getattr(claim, "confidence", 0.0) or 0.0),
        "vector_score": 0.0,
        "entity_score": 1.0,
        "source": "entity_fanout",
    }


def _row_for_vector_hit(claim, vector_score: float) -> dict:
    """Build a query_rows-shaped row dict for a Qdrant-sourced claim.

    ``vector_score`` is the raw Qdrant cosine similarity in [0, 1] (values
    below ``MEMORYMASTER_RECALL_VECTOR_SCORE_THRESHOLD`` are filtered out
    upstream). All other signals default to zero so ``W_VECTOR`` is the
    only thing promoting the row — at ``W_VECTOR=0`` (legacy default)
    these rows still add nothing to the ranking.
    """
    return {
        "claim": claim,
        "status": getattr(claim, "status", "confirmed"),
        "annotation": None,
        "score": 0.0,
        "lexical_score": 0.0,
        "freshness_score": 0.0,
        "confidence_score": float(getattr(claim, "confidence", 0.0) or 0.0),
        "vector_score": float(vector_score),
        "entity_score": 0.0,
        "source": "vector_fallback",
    }


def _apply_vector_fallback(
    svc,
    query: str,
    rows: list,
    seen_ids: set[int],
) -> list:
    """Augment ``rows`` with Qdrant semantic-search hits when the primary
    retrieval stages under-produced.

    Triggers only when ``len(rows) < MEMORYMASTER_RECALL_VECTOR_MIN_CANDIDATES``
    (default 3) and every env-var gate is satisfied. Silently degrades on
    any failure (qdrant unreachable, collection missing, embedder import
    error, etc) so the caller keeps whatever FTS5 + entity fanout produced.

    Returns the (possibly augmented) row list. Always mutates ``seen_ids``
    when new rows are added.
    """
    try:
        from memorymaster.recall import qdrant_recall_fallback
    except Exception as exc:  # pragma: no cover — import errors rare
        logger.debug("vector fallback: module import skipped: %s", exc)
        return rows

    if not qdrant_recall_fallback.is_fallback_enabled():
        return rows
    if len(rows) >= qdrant_recall_fallback.fallback_threshold():
        return rows

    try:
        hits = qdrant_recall_fallback.search(query)
    except Exception as exc:  # pragma: no cover — search() already swallows
        logger.debug("vector fallback: search skipped: %s", exc)
        return rows

    if not hits:
        return rows

    # Lazy security check — mirrors the entity fanout treatment.
    try:
        from memorymaster.core.security import is_sensitive_claim
    except Exception:
        is_sensitive_claim = lambda _claim: False  # type: ignore[assignment]  # noqa: E731

    appended = 0
    for hit in hits:
        cid = hit.claim_id
        if cid in seen_ids:
            continue
        try:
            claim = svc.store.get_claim(cid, include_citations=True)
        except Exception as exc:
            logger.debug("vector fallback: get_claim(%d) failed: %s", cid, exc)
            continue
        if claim is None or getattr(claim, "status", "") == "archived":
            continue
        if is_sensitive_claim(claim):
            continue
        rows.append(_row_for_vector_hit(claim, hit.score))
        seen_ids.add(cid)
        appended += 1

    if appended:
        logger.debug(
            "vector fallback: appended %d rows (total=%d) for query=%r",
            appended, len(rows), query[:60],
        )
    return rows


# Patterns that indicate something worth remembering
OBSERVATION_PATTERNS = [
    # User corrections/preferences
    (r"\b(don'?t|never|always|stop|instead|prefer|please)\b.*", "preference"),
    # Decisions
    (r"\b(decided|decision|we('ll| will)|let'?s|going to|plan is)\b.*", "decision"),
    # Constraints
    (r"\b(must|require|rule|constraint|forbidden|mandatory|critical)\b.*", "constraint"),
    # Architecture/tech choices
    (r"\b(using|switched to|migrated|deployed|installed|configured)\b.*", "fact"),
    # Bug/issue patterns
    (r"\b(bug|fix|broke|crash|error|issue|problem|wrong)\b.*", "event"),
    # Commitments
    (r"\b(todo|will do|next step|action item|need to|should)\b.*", "commitment"),
]

_COMPILED_PATTERNS = [(re.compile(p, re.IGNORECASE), t) for p, t in OBSERVATION_PATTERNS]


def classify_observation(text: str) -> str | None:
    """Check if text contains something worth remembering. Returns claim_type or None."""
    for pattern, claim_type in _COMPILED_PATTERNS:
        if pattern.search(text):
            return claim_type
    return None


def _emit_recall_latency(phase_ms: dict[str, float], total_start: float) -> None:
    """Emit per-stream + consolidated latency log lines for a recall() call.

    Emits one ``recall / latency`` line per stream that actually ran (streams
    absent from ``phase_ms`` emit nothing — zero-overhead invariant), plus a
    single ``recall / latency_total`` line with every phase consolidated so
    aggregators can pull all durations from one grep. Never raises —
    ``log_hook`` swallows internally, and this function is called from
    ``finally`` in ``recall()``.
    """
    try:
        total_ms = (time.perf_counter() - total_start) * 1000.0
        for stream, ms in phase_ms.items():
            log_hook("recall", _LATENCY_EVENT, stream=stream, ms=round(ms, 3))
        consolidated = {
            f"{stream}_ms": round(ms, 3) for stream, ms in phase_ms.items()
        }
        log_hook(
            "recall",
            _LATENCY_TOTAL_EVENT,
            total_ms=round(total_ms, 3),
            **consolidated,
        )
    except Exception:
        # Observation-only — never let logging break recall().
        pass


def recall(
    query: str,
    *,
    db_path: str = "",
    budget: int = 2000,
    format: str = "text",
    skip_qdrant: bool = False,
    return_ids: bool = False,
) -> str | tuple[str, list[int]]:
    """Query memorymaster for relevant context with quality ranking.

    By default returns the rendered ``# Memory Context`` markdown block
    (backwards-compatible). When ``return_ids=True``, returns a tuple of
    ``(markdown, [claim_id, ...])`` where the list is the ordered set of
    claim IDs that appear as bullets in the markdown — in the same order
    as rendered. The ID list is the audit-friendly output used by the
    eval harness so it no longer has to re-lookup rendered text against
    the DB (roadmap 11.7).

    ``return_ids`` defaults to ``False`` so every existing caller — MCP
    tools, CLI, hooks — gets the legacy ``str`` return type unchanged.
    """
    from memorymaster.core.service import MemoryService

    # Retrieval latency instrumentation (roadmap 5.1). ``phase_ms`` records
    # per-stream wall-clock durations in milliseconds. Each entry is emitted
    # as an individual ``recall / latency`` line AND consolidated into one
    # ``recall / latency_total`` line at the end. Streams that never run
    # (e.g. verbatim when ``MEMORYMASTER_RECALL_VERBATIM`` is unset) leave
    # no entry — that is how "zero overhead when disabled" is enforced.
    phase_ms: dict[str, float] = {}
    total_start = time.perf_counter()
    # When return_ids=True, _recall_impl appends rendered claim IDs here in
    # bullet-order so the caller gets exact mapping without parsing the
    # rendered markdown.
    rendered_ids: list[int] = []

    try:
        rendered = _recall_impl(
            query,
            db_path=db_path,
            budget=budget,
            format=format,
            skip_qdrant=skip_qdrant,
            phase_ms=phase_ms,
            _memory_service_cls=MemoryService,
            _rendered_ids=rendered_ids if return_ids else None,
        )
        if return_ids:
            return rendered, rendered_ids
        return rendered
    finally:
        _emit_recall_latency(phase_ms, total_start)


def query_for_task(
    task_description: str,
    project_scope: str,
    *,
    db_path: str = "",
    token_budget: int = 800,
    skip_qdrant: bool = True,
) -> str:
    """Look-ahead L1 — task-aware briefing for an upcoming PRD task.

    Wraps recall() with a task framing envelope so the receiving model
    sees the task context as a structured briefing block rather than
    generic memory recall.

    Parameters
    ----------
    task_description:
        Natural-language description of the upcoming task (e.g.
        "Implement OAuth callback in app/auth/google.ts").
    project_scope:
        Scope filter, e.g. ``"project:wezbridge"``. Only claims matching
        this scope (or its parents) are considered. If empty, no scope
        filter is applied (legacy behavior).
    db_path:
        DB to query (default: env MEMORYMASTER_DEFAULT_DB).
    token_budget:
        Max TOKENS of inner content (default 500 ≈ 2KB chars). Outer
        XML wrapper adds ~80 chars on top.
    skip_qdrant:
        Default True — vector search adds latency; FTS5 + ranking are
        sufficient for most task briefings.

    Returns
    -------
    str
        Rendered XML briefing of the form::

            <task_briefing project="..." budget_used="N">
              <task>...</task>
              <relevant_memory>
                # Memory Context
                - claim 1
                - claim 2
              </relevant_memory>
            </task_briefing>

        Empty string if no relevant claims found.
    """
    if not task_description:
        return ""

    # Sanitize for FTS5:
    # 1. Replace dashes/underscores/slashes with spaces so identifiers tokenize
    #    e.g. "memorymaster-recall" -> "memorymaster recall"
    # 2. FTS5 default is AND-match (all terms required), which over-filters when
    #    the query has common verbs ("extend", "to", "and") that don't appear
    #    in target claims. Convert to explicit OR between tokens of length >=3
    #    after dropping stop-words. This is closer to "find me any claim that
    #    mentions at least one of these distinctive terms."
    raw = re.sub(r"[-_/]+", " ", task_description)
    raw = re.sub(r"[^\w\s]", " ", raw)  # strip remaining punctuation
    tokens = [t for t in raw.split() if len(t) >= 3]
    _STOP = {
        "the", "and", "for", "with", "that", "this", "into", "from", "have",
        "has", "are", "was", "were", "will", "would", "should", "could",
        "via", "use", "uses", "using", "make", "makes", "via", "per",
        "extend", "implement", "build", "add", "fix", "update",
    }
    keep = [t.lower() for t in tokens if t.lower() not in _STOP]
    if not keep:
        return ""
    # Cap to 12 strongest tokens to keep FTS5 query bounded
    keep = list(dict.fromkeys(keep))[:12]
    sanitized = " OR ".join(keep)

    # Use MemoryService.query_for_context directly so we can pass scope_allowlist —
    # recall() doesn't expose that parameter. Scope filtering is essential for
    # task briefings: claims from other projects would be noise.
    from memorymaster.core.service import MemoryService

    db = db_path or os.environ.get("MEMORYMASTER_DEFAULT_DB") or "memorymaster.db"
    svc = MemoryService(db_target=db, workspace_root=Path.cwd())

    scope_filter = [project_scope] if project_scope else None
    # Query in legacy mode (FTS5 only) when skip_qdrant=True for latency.
    retrieval_mode = "legacy" if skip_qdrant else "hybrid"

    try:
        result = svc.query_for_context(
            sanitized,
            token_budget=token_budget,
            output_format="text",
            scope_allowlist=scope_filter,
            retrieval_mode=retrieval_mode,
        )
    except Exception:
        return ""

    inner = result.output if hasattr(result, "output") else str(result)
    if not inner or not inner.strip():
        return ""

    # Skip empty-result blocks: pack_context emits a "(no claims fit...)" placeholder.
    # We don't want to inject empty briefings; better to inject nothing.
    if "no claims fit" in inner.lower() or "0/0 claims" in inner:
        return ""

    # XML envelope. Escape minimal — recall output is markdown, not HTML.
    # We use CDATA-free wrapping; the model parses the whole block as text
    # and the tags give it structural cues.
    safe_task = task_description.replace("<", "&lt;").replace(">", "&gt;")
    safe_scope = project_scope.replace("<", "&lt;").replace(">", "&gt;")
    return (
        f'<task_briefing project="{safe_scope}" budget_used="{len(inner)}">\n'
        f'  <task>{safe_task}</task>\n'
        f'  <relevant_memory>\n'
        f'{inner}\n'
        f'  </relevant_memory>\n'
        f'</task_briefing>'
    )


def _recall_impl(
    query: str,
    *,
    db_path: str,
    budget: int,
    format: str,
    skip_qdrant: bool,
    phase_ms: dict[str, float],
    _memory_service_cls,
    _rendered_ids: list[int] | None = None,
) -> str:
    """Inner recall body. Kept separate from ``recall()`` so the outer
    function's ``try/finally`` can emit latency logs from every return path
    without reindenting the whole routine.

    When ``_rendered_ids`` is a list (not None), claim IDs are appended to
    it in bullet-order as the output markdown is built — enabling the
    ``return_ids=True`` opt-in on the public ``recall()`` API without
    re-running the ranker.
    """
    MemoryService = _memory_service_cls
    db = db_path or os.environ.get("MEMORYMASTER_DEFAULT_DB") or "memorymaster.db"
    if _wal_discipline_enabled():
        # P1 WAL-discipline (spec §2.2): the per-prompt recall hook must
        # never take a write lock on the shared multi-GB DB. The RO store
        # hands out mode=ro + query_only connections (side lookups follow
        # automatically through the store) and _record_accesses spools its
        # access/feedback signal for the steward drain instead of UPDATEing
        # inline — no tiering/decay/quality signal is lost (the F9 fix).
        svc = MemoryService(db_target=db, workspace_root=Path.cwd(), read_only=True)
    else:
        # Flag off = the untouched legacy RW path, bit-for-bit.
        svc = MemoryService(db_target=db, workspace_root=Path.cwd())

    # Pre-extract salient tokens before hitting FTS5. Passing the full
    # prompt verbatim AND-joins every token in FTS5 and rejects nearly all
    # real conversational prompts (see artifacts/retrieval-eval-2026-04-22).
    # FTS5 _escape_fts5_query() quotes-and-AND-joins tokens, so we instead
    # run one query per top token and union the results — effectively OR.
    from memorymaster.recall.recall_tokenizer import extract_query_tokens

    fts_query = extract_query_tokens(query, db, max_tokens=6)
    token_list = fts_query.split() if fts_query else []

    # Query expansion via entity-matched synonyms (roadmap 1.5). Opt-in via
    # MEMORYMASTER_RECALL_QUERY_EXPANSION=1. When enabled, we augment
    # ``token_list`` with alias tokens mined from the prompt's extracted
    # entities so the per-token FTS5 fanout below effectively runs an OR
    # clause across the expanded set. Default OFF so ranking is
    # bit-identical to legacy behaviour.
    if _query_expansion_enabled() and token_list:
        token_list = _apply_query_expansion(svc, query, token_list)

    rows: list = []
    seen_ids: set[int] = set()
    with _phase_timer(phase_ms, "fts5"):
        if token_list:
            # Fan out: top token first (highest IDF), then widen by OR.
            per_token_limit = max(3, 8 // max(1, len(token_list)))
            for tok in token_list:
                batch = svc.query_rows(
                    query_text=tok,
                    limit=per_token_limit,
                    retrieval_mode="legacy",
                    include_candidates=True,
                    scope_allowlist=None,
                )
                for row in batch:
                    claim = row.get("claim")
                    cid = getattr(claim, "id", None)
                    if cid is None or cid in seen_ids:
                        continue
                    seen_ids.add(cid)
                    rows.append(row)
                if len(rows) >= 8:
                    break

        if not rows:
            # Fallback to raw prompt — preserves the old behaviour.
            rows = svc.query_rows(
                query_text=query,
                limit=8,
                retrieval_mode="legacy",
                include_candidates=True,
                scope_allowlist=None,
            )
            for row in rows:
                claim = row.get("claim")
                cid = getattr(claim, "id", None)
                if cid is not None:
                    seen_ids.add(cid)

    # Entity-link fanout — mine entities from the prompt, resolve via
    # entity_aliases, and union in claims we haven't already seen.
    #
    # Backwards-compat contract: when MEMORYMASTER_RECALL_W_ENTITY == 0.0
    # (shipped default) the fanout ONLY runs if the FTS5 stage returned
    # nothing — it acts purely as a rescue path for zero-hit prompts, which
    # keeps ranking bit-identical for the 24/30 prompts that already hit.
    # When W_ENTITY > 0, fanout runs unconditionally and its rows (with
    # entity_score=1.0, other scores zeroed) contribute to the re-rank.
    w_entity_probe = _recall_weight("W_ENTITY")
    should_fanout = (not rows) or (w_entity_probe > 0.0)
    # Only time + emit when the stream actually runs, so disabled streams
    # contribute no latency line (zero-overhead invariant, roadmap 5.1).
    if should_fanout:
        with _phase_timer(phase_ms, "entity_fanout"):
            # Lazy import so legacy callers without the security module still
            # work — fanout is a best-effort layer.
            try:
                from memorymaster.core.security import is_sensitive_claim
            except Exception:
                is_sensitive_claim = lambda _claim: False  # type: ignore[assignment]  # noqa: E731
            fanout_ids = _entity_fanout_claim_ids(svc.store, query, seen_ids)
            for cid in fanout_ids:
                try:
                    claim = svc.store.get_claim(cid, include_citations=True)
                except Exception:
                    continue
                if claim is None or getattr(claim, "status", "") == "archived":
                    continue
                if is_sensitive_claim(claim):
                    continue
                rows.append(_row_for_claim(claim))

    # Vector fallback — Qdrant semantic search when FTS5 + entity fanout
    # produced fewer than MEMORYMASTER_RECALL_VECTOR_MIN_CANDIDATES rows
    # (default 3). Fully env-gated so default behaviour is unchanged. See
    # ``_apply_vector_fallback`` for the exact gating logic. Only time when
    # the fallback is enabled — otherwise we emit nothing (zero-overhead).
    _vector_enabled = False
    try:
        from memorymaster.recall import qdrant_recall_fallback as _qrf

        _vector_enabled = bool(_qrf.is_fallback_enabled())
    except Exception:
        _vector_enabled = False

    if _vector_enabled:
        with _phase_timer(phase_ms, "vector_fallback"):
            rows = _apply_vector_fallback(svc, query, rows, seen_ids)
    else:
        rows = _apply_vector_fallback(svc, query, rows, seen_ids)

    # Verbatim retrieval — MemPalace-style raw conversation stream.
    #
    # Gated on MEMORYMASTER_RECALL_VERBATIM=1 (default 0 = off) so legacy
    # behaviour is bit-identical when the env var is absent.
    #
    # When a verbatim hit's scope matches a claim we already retrieved,
    # we BOOST that claim's verbatim_score rather than add a synthetic
    # row — avoids phantom candidates when the information is already
    # represented as a curated claim.
    try:
        from memorymaster.recall.verbatim_recall import (
            hit_to_synthetic_row,
            is_enabled as _verbatim_enabled,
            recall_verbatim,
        )
    except Exception as _verbatim_import_exc:  # pragma: no cover - importless path
        # v3.9.1 S1 — surface this rather than silently using no-op lambdas.
        # User needs to know that even if MEMORYMASTER_RECALL_VERBATIM=1 is set,
        # the stream is unavailable because the install is incomplete or the
        # module raised at import time. Logged at WARNING so it surfaces in
        # default log levels but doesn't crash recall().
        if not _VERBATIM_IMPORT_WARNED:
            logger.warning(
                "verbatim_recall import failed (%s) — verbatim stream is OFF "
                "regardless of MEMORYMASTER_RECALL_VERBATIM. Reinstall "
                "memorymaster or check the import error.",
                _verbatim_import_exc,
            )
            globals()["_VERBATIM_IMPORT_WARNED"] = True
        _verbatim_enabled = lambda: False  # type: ignore[assignment]  # noqa: E731
        recall_verbatim = lambda *a, **k: []  # type: ignore[assignment]  # noqa: E731
        hit_to_synthetic_row = None  # type: ignore[assignment]

    # Verbatim stream is opt-in — time + emit only when enabled so the
    # disabled case produces zero latency lines (roadmap 5.1).
    if _verbatim_enabled():
        with _phase_timer(phase_ms, "verbatim"):
            try:
                verbatim_hits = recall_verbatim(query, scope=None, db_path=db, limit=5)
            except Exception as exc:
                logger.debug("verbatim stream skipped: %s", exc)
                verbatim_hits = []

            if verbatim_hits and hit_to_synthetic_row is not None:
                scope_to_rows: dict[str, list[dict]] = {}
                for row in rows:
                    claim = row.get("claim")
                    s = getattr(claim, "scope", "") or ""
                    if not s:
                        continue
                    scope_to_rows.setdefault(s, []).append(row)

                added_excerpts: set[str] = set()
                for hit in verbatim_hits:
                    existing = scope_to_rows.get(hit.scope) or []
                    if existing:
                        target = existing[0]
                        prev = float(target.get("verbatim_score") or 0.0)
                        if hit.score > prev:
                            target["verbatim_score"] = hit.score
                            target["_verbatim_id"] = hit.verbatim_id
                        continue
                    key = hit.excerpt[:100]
                    if key in added_excerpts:
                        continue
                    added_excerpts.add(key)
                    rows.append(hit_to_synthetic_row(hit))

    # Graph traversal stream (roadmap 11.3) — annotate candidate rows
    # whose claim_id is reachable within MAX_HOPS of the query's entities
    # in the Kuzu graph. Opt-in via MEMORYMASTER_RECALL_GRAPH=1. When off,
    # the entire stream short-circuits in ``_graph_reached_claim_ids``
    # before any Kuzu import, so there's zero overhead on the default
    # code path. When on but the graph DB is empty / missing / corrupt,
    # the helper returns an empty set (claim 11907 silent-fail pattern)
    # and graph_score stays 0.0 on every row — equivalent to the stream
    # being disabled.
    #
    # Only time + emit latency when the stream is ON so disabled callers
    # produce no latency line (zero-overhead invariant, roadmap 5.1).
    # Two-pass stream (v3.9.0 F5, gbrain v0.21 "Cathedral II"-style).
    # Gated on MEMORYMASTER_RECALL_TWO_PASS=1. Walks entity_aliases to find
    # claims that share entities with the already-recalled seeds. Adds them
    # as new rows annotated with ``two_pass_score = 1.0`` (single-hop) — a
    # future revision could cap, dedup more aggressively, or weight by hop
    # count if depth > 1 is enabled.
    if _two_pass_enabled():
        with _phase_timer(phase_ms, "two_pass"):
            try:
                seed_ids = [
                    int(c.id)
                    for r in rows
                    if (c := r.get("claim")) is not None
                    and getattr(c, "id", None) is not None
                ][:10]  # cap seeds so the fanout stays bounded
                neighbor_ids = _two_pass_neighbor_ids(svc.store, seed_ids, set(seen_ids))
                neighbor_distances: dict[int, int] = {n: 1 for n in neighbor_ids}
                # v3.10 W2 — also walk claim_edges (F8 structural edges)
                # when MEMORYMASTER_RECALL_TWO_PASS_USE_EDGES=1. Distances
                # from the structural walk OVERRIDE entity-fanout where
                # they're shorter (closer neighbors via explicit references).
                if _two_pass_use_edges() and seed_ids:
                    try:
                        from memorymaster.recall.claim_edges import walk_neighbors as _walk_edges
                        edge_distances = _walk_edges(db, seed_ids, max_hops=2)
                        for nid, hops in edge_distances.items():
                            existing = neighbor_distances.get(nid)
                            if existing is None or hops < existing:
                                neighbor_distances[nid] = int(hops)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("two_pass edges walk skipped: %s", exc)
                for nid, hops in neighbor_distances.items():
                    if nid in seen_ids:
                        continue
                    try:
                        claim = svc.store.get_claim(nid)
                    except Exception:
                        claim = None
                    if claim is None:
                        continue
                    # Distance-decayed score so closer neighbors weigh more.
                    score = 1.0 / (1.0 + float(hops))
                    rows.append(
                        {
                            "claim": claim,
                            "lexical_score": 0.0,
                            "freshness_score": 0.0,
                            "confidence_score": float(getattr(claim, "confidence", 0.0) or 0.0),
                            "vector_score": 0.0,
                            "entity_score": 0.0,
                            "two_pass_score": score,
                            "source": "two_pass",
                        }
                    )
                    seen_ids.add(nid)
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug("two_pass stream skipped: %s", exc)

    # Closets stream (v3.10 W1, MemPalace v3.3.0). Gated on
    # MEMORYMASTER_RECALL_CLOSETS=1. Calls search_closets(query) → for each
    # matched wiki article, hydrates its claim_ids as new rows annotated with
    # ``closet_score = 1.0``. When a closet-hydrated claim is already in
    # ``rows`` we BOOST its closet_score on the existing row instead of
    # adding a duplicate.
    if _closets_enabled():
        with _phase_timer(phase_ms, "closets"):
            try:
                from memorymaster.knowledge.closets import search_closets as _search_closets
            except Exception as exc:  # noqa: BLE001 — module may be missing
                logger.debug("closets stream skipped (import): %s", exc)
                _search_closets = None
            if _search_closets is not None:
                try:
                    # v3.11 P1 fix: cap to 3 articles (was 5) to bound the
                    # candidate flood, and request BM25-normalised scores so
                    # weaker matches don't dominate ranking with constant 1.0.
                    closet_hits = _search_closets(db, query, limit=3, with_scores=True)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("closets stream skipped (search): %s", exc)
                    closet_hits = []
                if closet_hits:
                    existing_by_id: dict[int, dict] = {}
                    for row in rows:
                        c = row.get("claim")
                        if c is not None and getattr(c, "id", None) is not None:
                            existing_by_id[int(c.id)] = row
                    boost_only = _closets_boost_only()
                    for hit in closet_hits:
                        if len(hit) == 3:
                            _slug, claim_ids, score = hit
                        else:
                            _slug, claim_ids = hit
                            score = 1.0
                        for cid in claim_ids:
                            if cid in existing_by_id:
                                prev = float(existing_by_id[cid].get("closet_score") or 0.0)
                                existing_by_id[cid]["closet_score"] = max(prev, float(score))
                                continue
                            if boost_only:
                                # v3.11 P1b — never hydrate new candidates;
                                # closets are pure re-ranking. Skip cids not
                                # already in the candidate set.
                                continue
                            try:
                                claim = svc.store.get_claim(cid)
                            except Exception:
                                claim = None
                            if claim is None:
                                continue
                            rows.append(
                                {
                                    "claim": claim,
                                    "lexical_score": 0.0,
                                    "freshness_score": 0.0,
                                    "confidence_score": float(getattr(claim, "confidence", 0.0) or 0.0),
                                    "vector_score": 0.0,
                                    "entity_score": 0.0,
                                    "closet_score": float(score),
                                    "source": "closets",
                                }
                            )
                            seen_ids.add(cid)
                            existing_by_id[cid] = rows[-1]

    if _graph_enabled():
        with _phase_timer(phase_ms, "graph"):
            # Roadmap 12.1 — distance-weighted score breaks the
            # constant-bonus pathology of the boolean stream. Closer
            # claims get a much bigger boost than far ones:
            #   hop 0  → score 1.000  (claim mentions a query entity)
            #   hop 1  → score 0.500
            #   hop 2  → score 0.333
            #   not reached → score 0.0
            # Wrap the whole stream so a catastrophic boundary failure
            # (BFS helper, store, or harvest hydrate raising) is logged and
            # swallowed — the graph stream NEVER raises into recall (claim
            # 11907). The helpers already swallow their own internal errors;
            # this is the outer belt-and-suspenders guard the spec mandates.
            try:
                graph_distance = _graph_reached_claim_distance(query, svc.store)
                if graph_distance:
                    for row in rows:
                        claim = row.get("claim")
                        cid = getattr(claim, "id", None)
                        if cid is not None and int(cid) in graph_distance:
                            hops = graph_distance[int(cid)]
                            row["graph_score"] = 1.0 / (1.0 + float(hops))
                        elif row.get("graph_score") is None:
                            row["graph_score"] = 0.0
                    # Roadmap 12.2 — HARVEST path. When
                    # MEMORYMASTER_RECALL_GRAPH_CANDIDATES=1, promote the
                    # stream from annotation-only to a full retrieval stream:
                    # BFS-reached claims NOT already in the candidate pool are
                    # hydrated as new rows. Default OFF keeps the row set
                    # bit-identical.
                    if _graph_candidates_enabled():
                        _harvest_graph_rows(
                            svc, graph_distance, rows, seen_ids
                        )
            except Exception as exc:  # noqa: BLE001 — defensive (claim 11907)
                logger.debug("graph stream skipped: %s", exc)

    if not rows and not skip_qdrant:
        # Fallback to Qdrant semantic search
        try:
            from memorymaster.recall.qdrant_backend import QdrantBackend
            backend = QdrantBackend()
            hits = backend.search(query, limit=5)
            backend.close()
            if hits:
                lines = ["# Memory Context (semantic)", ""]
                for hit in hits:
                    p = hit.get("payload", {})
                    text = p.get("claim_text", "")[:200]
                    lines.append(f"- {text}")
                    if _rendered_ids is not None:
                        cid = p.get("claim_id")
                        if isinstance(cid, int):
                            _rendered_ids.append(cid)
                return "\n".join(lines).encode("ascii", errors="replace").decode("ascii")
        except Exception:
            pass
        return ""

    if not rows:
        return ""

    # Re-rank by lexical relevance — claims with more query words score higher.
    # Use the tokenized query (same terms we actually sent to FTS5) so the
    # post-ranker agrees with retrieval.
    query_words = set(fts_query.lower().split()) or set(query.lower().split())

    # Resolve weights once per call — env overrides shipped defaults.
    w_matches = _recall_weight("W_MATCHES")
    w_phrase = _recall_weight("W_PHRASE")
    w_all = _recall_weight("W_ALL")
    w_lexical = _recall_weight("W_LEXICAL")
    w_confidence = _recall_weight("W_CONFIDENCE")
    w_freshness = _recall_weight("W_FRESHNESS")
    w_vector = _recall_weight("W_VECTOR")
    w_entity = _recall_weight("W_ENTITY")
    w_verbatim = _recall_weight("W_VERBATIM")
    # W_GRAPH (roadmap 11.3) — only contributes when the graph stream was
    # enabled AND reached at least one row. Default 0.0 preserves
    # bit-identical ranking with the 5-stream baseline.
    w_graph = _recall_weight("W_GRAPH")
    w_two_pass = _recall_weight("W_TWO_PASS")
    w_closets = _recall_weight("W_CLOSETS")
    # W_CLAIM_TYPE (v3.9.0 F1, MemPalace "Halls"-inspired). When > 0, classify
    # the query via classify_observation() and look up its inferred claim_type;
    # rows whose claim.claim_type matches get a (1 + w_claim_type) multiplier
    # at the end of _relevance. Default 0.0 → query_type stays None and the
    # boost is a no-op, preserving bit-identical ranking.
    w_claim_type = _recall_weight("W_CLAIM_TYPE")
    # v3.11 P2 — derive query intent from query_classifier (sharper than the
    # 6-pattern classify_observation which matched "preference" too greedily).
    # Map query_type → claim_type so the boost compares apples to apples.
    if w_claim_type > 0.0:
        try:
            from memorymaster.recall.query_classifier import classify_query
            qt = classify_query(query)
            _query_to_claim_type = {
                "constraint_check": "constraint",
                "preference": "preference",
                "fact_lookup": "fact",
                "verification": "fact",
                "temporal": "event",
                "relational": "fact",
                "open_ended": None,  # too vague to bias
            }
            query_claim_type = _query_to_claim_type.get(qt)
        except Exception:  # noqa: BLE001
            query_claim_type = classify_observation(query)
    else:
        query_claim_type = None
    # Scope-aware retrieval boost (roadmap 1.2). Multiplier applied to the
    # final _relevance score for claims whose scope matches the current
    # project scope. 0.0 (default) → no boost, ranking bit-identical to legacy.
    scope_boost = _recall_scope_boost()
    current_scope = _current_scope() if scope_boost > 0.0 else ""

    # Build BM25 corpus stats over the candidate set once per call. This is
    # cheap (O(rows * avg_doc_len)) and strictly read-only — we never touch
    # the DB past what query_rows already fetched. Feature-flagged; on by
    # default after the sweep. See module-level comment for why.
    # Time + emit only when BM25 is active — an operator disabling BM25 via
    # MEMORYMASTER_LEXICAL_BM25=0 should see no latency line for this stream.
    bm25_on = _bm25_enabled()
    bm25_scores: dict[int, float] = {}
    _bm25_start = time.perf_counter() if bm25_on else None
    if bm25_on:
        from memorymaster.recall.recall_tokenizer import _candidate_tokens

        def _doc_tokens(raw) -> list[str]:
            """Tokenize ONE field (subject or text) with the standard filter.

            Keeps the >=3 len filter we've used since the BM25 sweep, so
            per-field results are comparable to the concatenated baseline
            for shared tokens. Non-string inputs (missing attrs, MagicMock
            placeholders in tests) are coerced to empty string — never crash
            the rescorer.
            """
            if not isinstance(raw, str):
                return []
            return [t for t in _candidate_tokens(raw) if len(t) >= 3]

        # Cache per-field tokenisation per row and build per-field df + dl
        # stats. We compute BM25 on each field independently and then combine
        # with per-field weights, so a rare subject match is not diluted by a
        # long text body (which was the original failure mode of the
        # concatenated scorer).
        subj_tokens_by_id: dict[int, list[str]] = {}
        text_tokens_by_id: dict[int, list[str]] = {}
        df_subj: dict[str, int] = {}
        df_text: dict[str, int] = {}
        for r in rows:
            c = r.get("claim")
            cid = getattr(c, "id", None)
            if cid is None or cid in subj_tokens_by_id:
                continue
            subj_toks = _doc_tokens(getattr(c, "subject", "") or "")
            text_toks = _doc_tokens(getattr(c, "text", "") or "")
            subj_tokens_by_id[cid] = subj_toks
            text_tokens_by_id[cid] = text_toks
            for t in set(subj_toks):
                df_subj[t] = df_subj.get(t, 0) + 1
            for t in set(text_toks):
                df_text[t] = df_text.get(t, 0) + 1

        n_docs = len(subj_tokens_by_id)
        # avg_dl is field-specific — a field with mostly empty strings (dl=0)
        # gets an avg_dl that reflects only the non-empty docs. When every
        # doc is empty for a field, avg_dl stays 0 and that field contributes
        # nothing (the per-doc branch below skips dl==0).
        non_empty_subj = [v for v in subj_tokens_by_id.values() if v]
        non_empty_text = [v for v in text_tokens_by_id.values() if v]
        avg_dl_subj = (
            sum(len(v) for v in non_empty_subj) / len(non_empty_subj)
            if non_empty_subj else 0.0
        )
        avg_dl_text = (
            sum(len(v) for v in non_empty_text) / len(non_empty_text)
            if non_empty_text else 0.0
        )

        k1 = _bm25_param("K1", _BM25_K1_DEFAULT)
        b = _bm25_param("B", _BM25_B_DEFAULT)
        w_subject = _bm25_field_weight("W_SUBJECT", _BM25_W_SUBJECT_DEFAULT)
        w_text = _bm25_field_weight("W_TEXT", _BM25_W_TEXT_DEFAULT)

        # Query tokens for BM25: use the same tokenizer as the tokenizer
        # pipeline so we agree with the retrieval stage. Fall back to the
        # raw query_words split when the tokenizer finds nothing. Shared
        # across both fields — the df is per-field so IDF still varies.
        q_tokens = [t for t in _candidate_tokens(query) if len(t) >= 3]
        if not q_tokens:
            q_tokens = [w for w in query_words if len(w) >= 3]

        def _bm25_field_score(
            doc_tokens: list[str],
            df_field: dict[str, int],
            avg_dl_field: float,
        ) -> float:
            """BM25 for one field on one doc. Returns 0.0 when the field is empty."""
            if not doc_tokens or avg_dl_field <= 0.0:
                return 0.0
            tf: dict[str, int] = {}
            for t in doc_tokens:
                tf[t] = tf.get(t, 0) + 1
            dl = len(doc_tokens)
            score = 0.0
            for qt in q_tokens:
                f = tf.get(qt, 0)
                if f == 0:
                    continue
                # IDF uses n_docs from the corpus (shared across fields) but
                # df_field from this field only. A term present in every
                # subject but no text body still contributes via the text
                # stream when it appears there with a high IDF.
                n_q = df_field.get(qt, 0)
                idf = math.log(
                    ((n_docs - n_q + 0.5) / (n_q + 0.5)) + 1.0
                )
                norm = 1.0 - b + b * (dl / avg_dl_field)
                score += idf * ((f * (k1 + 1.0)) / (f + k1 * norm))
            return score

        if n_docs > 0 and q_tokens:
            for cid in subj_tokens_by_id:
                subj_score = _bm25_field_score(
                    subj_tokens_by_id[cid], df_subj, avg_dl_subj
                )
                text_score = _bm25_field_score(
                    text_tokens_by_id[cid], df_text, avg_dl_text
                )
                combined = w_subject * subj_score + w_text * text_score
                if combined > 0.0:
                    bm25_scores[cid] = combined

    if _bm25_start is not None:
        # Manually close the BM25 timer (we opened it with a perf_counter
        # snapshot above rather than a context manager because the body has
        # too many local function definitions to reindent cleanly).
        phase_ms["bm25_rescore"] = (time.perf_counter() - _bm25_start) * 1000.0

    # Rank + output-budget loop (roadmap 5.1 stream "rank_and_build"). This
    # phase ALWAYS runs when ``rows`` is non-empty, so it always emits a
    # latency line — the metric is useful as a baseline against the other
    # streams. Starts AFTER BM25 closes so the two don't double-count.
    _rank_start = time.perf_counter()

    def _relevance(row):
        claim = row.get("claim")
        text = (claim.text if hasattr(claim, "text") else "").lower()
        # Count how many query words (length > 2) appear in the claim text.
        tokens_gt2 = [w for w in query_words if len(w) > 2]
        matches = sum(1 for w in tokens_gt2 if w in text)
        # Bonus: full query phrase appears in text.
        phrase_bonus = 1.0 if query.lower() in text else 0.0
        # Bonus: ALL query tokens present (not just some).
        all_present = 1.0 if tokens_gt2 and matches == len(tokens_gt2) else 0.0
        if bm25_on:
            cid = getattr(claim, "id", None)
            lexical = bm25_scores.get(cid, 0.0) if cid is not None else 0.0
        else:
            lexical = float(row.get("lexical_score") or 0.0)
        conf = float(row.get("confidence_score") or 0.0)
        freshness = float(row.get("freshness_score") or 0.0)
        vector = float(row.get("vector_score") or 0.0)
        # entity_score is 1.0 for fanout-sourced claims, absent (→0.0) for
        # FTS5-sourced rows. When W_ENTITY==0.0 this contributes nothing,
        # preserving bit-identical ranking with the pre-fanout implementation.
        entity = float(row.get("entity_score") or 0.0)
        # verbatim_score is non-zero only when the verbatim stream is gated
        # on (MEMORYMASTER_RECALL_VERBATIM=1) AND the row matched a FTS5
        # query over verbatim_memories. W_VERBATIM=0.0 (default) preserves
        # legacy ranking bit-for-bit.
        verbatim = float(row.get("verbatim_score") or 0.0)
        # graph_score is 1.0 for rows whose claim_id was reachable within
        # MAX_HOPS of the query's entities on the Kuzu graph; 0.0 (or
        # absent) otherwise. W_GRAPH=0.0 (default) makes this a no-op so
        # legacy ranking is bit-identical.
        graph = float(row.get("graph_score") or 0.0)
        # two_pass_score is non-zero only when the row was added via the
        # F5 two-pass entity-fanout stream. W_TWO_PASS=0.0 (default) makes
        # this contribute nothing even when the stream is on.
        two_pass = float(row.get("two_pass_score") or 0.0)
        # closet_score is non-zero only when the row was added/boosted via
        # the v3.10 W1 closets stream. W_CLOSETS=0.0 (default) makes this
        # contribute nothing even when MEMORYMASTER_RECALL_CLOSETS=1.
        closets = float(row.get("closet_score") or 0.0)
        base = (
            matches * w_matches
            + phrase_bonus * w_phrase
            + all_present * w_all
            + lexical * w_lexical
            + conf * w_confidence
            + freshness * w_freshness
            + vector * w_vector
            + entity * w_entity
            + verbatim * w_verbatim
            + graph * w_graph
            + two_pass * w_two_pass
            + closets * w_closets
        )
        # claim_type-aware boost (v3.9.0 F1). When w_claim_type > 0 AND the
        # query was classified into a type AND this claim's claim_type matches,
        # multiply by (1.0 + w_claim_type). Default w_claim_type=0.0 makes
        # query_claim_type stay None so this branch is a no-op.
        if query_claim_type is not None:
            row_type = (getattr(claim, "claim_type", "") or "").strip().lower()
            if row_type == query_claim_type:
                base = base * (1.0 + w_claim_type)
        # Scope-aware retrieval boost (roadmap 1.2). When scope_boost > 0 AND
        # the claim's scope matches the current project scope, multiply by
        # (1.0 + scope_boost). When scope_boost == 0 this branch is a no-op
        # (current_scope is "") so ranking is bit-identical to legacy.
        if scope_boost > 0.0:
            claim_scope = getattr(claim, "scope", "") or ""
            if claim_scope == current_scope:
                return base * (1.0 + scope_boost)
        return base

    # Fusion mode: linear (default, legacy bit-identical), rrf, or auto.
    # RRF is Reciprocal Rank Fusion — merges per-stream rankings instead of
    # summing weighted raw scores. See memorymaster/recall_fusion.py.
    # ``auto`` applies a stream-topology heuristic (roadmap 11.6, claim
    # 11898): pick RRF when >= AUTO_GATE_THRESHOLD (default 3) streams are
    # populated, else linear. Default stays ``linear`` so legacy callers
    # are bit-identical.
    fusion_mode = os.environ.get("MEMORYMASTER_RECALL_FUSION", "linear").strip().lower()

    if fusion_mode == "auto":
        fusion_mode, _, _ = _auto_gate_decide(
            rows,
            bm25_scores,
            bm25_on,
            w_freshness,
            query=query,
        )

    if fusion_mode == "rrf":
        from memorymaster.recall.recall_fusion import rrf_fuse

        # Build per-stream rankings — skip any stream where ALL rows score zero
        # so a disabled stream (e.g. vector when Qdrant is off) contributes
        # nothing rather than a deterministic but meaningless order.
        def _row_cid(r: dict) -> int | None:
            c = r.get("claim")
            return getattr(c, "id", None)

        def _ranking(score_fn) -> list[int]:
            scored = [(cid, score_fn(r)) for r in rows
                      if (cid := _row_cid(r)) is not None]
            if all(s == 0.0 for _, s in scored):
                return []
            scored.sort(key=lambda x: x[1], reverse=True)
            return [cid for cid, _ in scored]

        rankings: dict[str, list[int]] = {}
        bm25_ranking = _ranking(
            lambda r: bm25_scores.get(_row_cid(r), 0.0) if bm25_on else 0.0
        )
        if bm25_ranking:
            rankings["bm25"] = bm25_ranking
        entity_ranking = _ranking(lambda r: float(r.get("entity_score") or 0.0))
        if entity_ranking:
            rankings["entity"] = entity_ranking
        vector_ranking = _ranking(lambda r: float(r.get("vector_score") or 0.0))
        if vector_ranking:
            rankings["vector"] = vector_ranking
        verbatim_ranking = _ranking(lambda r: float(r.get("verbatim_score") or 0.0))
        if verbatim_ranking:
            rankings["verbatim"] = verbatim_ranking
        freshness_ranking = _ranking(lambda r: float(r.get("freshness_score") or 0.0))
        if freshness_ranking:
            rankings["freshness"] = freshness_ranking
        # Graph stream — contributes to RRF only when at least one row got
        # a non-zero graph_score (i.e. MEMORYMASTER_RECALL_GRAPH=1 fired
        # and the graph reached the candidate pool).
        graph_ranking = _ranking(lambda r: float(r.get("graph_score") or 0.0))
        if graph_ranking:
            rankings["graph"] = graph_ranking

        if rankings:
            rrf_scores = rrf_fuse(rankings)
            # Rows without any stream score (shouldn't normally happen) sink
            # to the bottom via default 0.0. Keep stable order for ties.
            ranked = sorted(
                rows,
                key=lambda r: rrf_scores.get(_row_cid(r) or -1, 0.0),
                reverse=True,
            )
        else:
            # No active streams — fall through to linear so we still return
            # a deterministic order (same as legacy behaviour).
            ranked = sorted(rows, key=_relevance, reverse=True)
    else:
        ranked = sorted(rows, key=_relevance, reverse=True)

    # Build output — top claims within budget
    lines = ["# Memory Context", ""]
    tokens_used = 0
    chars_per_token = 4
    for row in ranked:
        claim = row.get("claim")
        if not hasattr(claim, "text"):
            continue
        text = claim.text[:300]
        wiki_slug = getattr(claim, "wiki_article", None)
        if wiki_slug:
            chunk = f"- {text}  (compiled in [[{wiki_slug}]])"
        else:
            chunk = f"- {text}"
        chunk_tokens = len(chunk) // chars_per_token
        if tokens_used + chunk_tokens > budget:
            break
        lines.append(chunk)
        tokens_used += chunk_tokens
        if _rendered_ids is not None:
            cid = getattr(claim, "id", None)
            if isinstance(cid, int):
                _rendered_ids.append(cid)

    # Close the rank_and_build timer right before we emit output so the
    # measurement covers _relevance/RRF + the budget-trimming loop.
    phase_ms["rank_and_build"] = (time.perf_counter() - _rank_start) * 1000.0

    if len(lines) <= 2:
        return ""

    return "\n".join(lines).encode("ascii", errors="replace").decode("ascii")


def observe(
    text: str,
    *,
    source: str = "session",
    db_path: str = "",
    scope: str | None = None,
    auto_classify: bool = True,
    force: bool = False,
) -> dict:
    """Extract and ingest observations from conversation text.

    If auto_classify=True, only ingests text that matches observation patterns.
    If force=True, ingests regardless of pattern matching.

    `scope` defaults to scope_from_cwd(Path.cwd()) when not given — produces
    `project:<slug>` matching the rest of the system. Previous default was the
    literal string `"project"` which created orphan claims unreachable from
    any `project:<slug>` recall path (overnight audit F-5, mm-d24c context).

    Returns: {"ingested": bool, "claim_type": str, "claim_id": int | None}
    """
    if scope is None:
        from memorymaster.core.scope_utils import scope_from_cwd
        scope = scope_from_cwd(Path.cwd())
    # Check if worth remembering
    claim_type = None
    if auto_classify:
        claim_type = classify_observation(text)
        if claim_type is None and not force:
            return {"ingested": False, "claim_type": None, "claim_id": None, "reason": "no_pattern_match"}

    if not claim_type:
        claim_type = "fact"

    from memorymaster.core.models import CitationInput
    from memorymaster.core.service import MemoryService

    db = db_path or os.environ.get("MEMORYMASTER_DEFAULT_DB") or "memorymaster.db"
    svc = MemoryService(db_target=db, workspace_root=Path.cwd())

    try:
        claim = svc.ingest(
            text=text.strip()[:2000],
            citations=[CitationInput(source=source)],
            claim_type=claim_type,
            scope=scope,
            confidence=0.6,
            source_agent="context-hook",
        )
        return {"ingested": True, "claim_type": claim_type, "claim_id": claim.id}
    except Exception as exc:
        logger.warning("Observe failed: %s", exc)
        return {"ingested": False, "claim_type": claim_type, "claim_id": None, "reason": str(exc)}


def observe_llm(
    text: str,
    *,
    source: str = "session",
    db_path: str = "",
    scope: str | None = None,
) -> dict:
    """Use LLM to extract structured claims from conversation text.

    More thorough than rule-based observe() but slower (~5s per call).
    `scope` defaults to scope_from_cwd(Path.cwd()) when not given — see
    observe() docstring for the F-5 background.
    """
    if scope is None:
        from memorymaster.core.scope_utils import scope_from_cwd
        scope = scope_from_cwd(Path.cwd())
    from memorymaster.knowledge.auto_extractor import extract_claims_from_text
    from memorymaster.core.models import CitationInput
    from memorymaster.core.service import MemoryService

    db = db_path or os.environ.get("MEMORYMASTER_DEFAULT_DB") or "memorymaster.db"

    extracted = extract_claims_from_text(text, source=source, scope=scope)
    if not extracted:
        return {"ingested": 0, "extracted": 0}

    svc = MemoryService(db_target=db, workspace_root=Path.cwd())
    ingested = 0
    for claim_data in extracted:
        try:
            svc.ingest(
                text=claim_data.get("text", ""),
                citations=[CitationInput(source=source)],
                claim_type=claim_data.get("claim_type", "fact"),
                subject=claim_data.get("subject"),
                predicate=claim_data.get("predicate"),
                object_value=claim_data.get("object_value"),
                scope=scope,
                confidence=0.6,
                source_agent="context-hook-llm",
            )
            ingested += 1
        except Exception:
            pass

    return {"ingested": ingested, "extracted": len(extracted)}
