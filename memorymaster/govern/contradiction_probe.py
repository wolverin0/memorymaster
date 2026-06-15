"""Suspected-contradictions probe (v3.22, ported from gbrain v0.32.6).

MemoryMaster's deterministic conflict detection (conflict_resolver,
jobs/dedup.find_conflicts) only catches claims with the SAME subject+predicate
and a different object_value. It misses *semantic* contradictions phrased
differently — e.g. "the API is rate-limited at 100 req/min" vs "there is no
rate limit on the API". This probe finds those:

1. Sample topically-similar claim pairs via embedding cosine similarity in a
   band (similar enough to be about the same thing, not near-duplicates).
2. Cheap pre-filter to skip pairs the deterministic path owns or that are
   already linked by supersession.
3. Ask an LLM whether the pair genuinely contradicts (severity-scored), with a
   persistent verdict cache so re-runs don't re-pay.
4. Report a contradiction rate with a Wilson 95% confidence interval (judge
   errors counted in the denominator so the rate stays honest).

It does NOT auto-resolve. Default is a dry-run report; ``apply=True`` flags the
lower-confidence claim of each contradicting pair as ``conflicted`` (the
needs-human-arbitration state) via the lifecycle helper — never raw SQL, never
archive/supersede.
"""
from __future__ import annotations

import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from memorymaster.core import llm_provider
from memorymaster.core import llm_budget
from memorymaster.stores._storage_shared import open_conn
from memorymaster.recall.embeddings import EmbeddingProvider, cosine_similarity, create_best_provider
from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import Claim
from memorymaster.core.security import redact_text

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
_SKIP_STATUSES = {"superseded", "archived"}

_PROMPT = """You compare two memory claims and decide if they CONTRADICT each other.
A contradiction means both cannot be true at the same time about the same thing.
Topically related but compatible claims do NOT contradict. Different subjects do
NOT contradict.

Output ONE JSON object and nothing else:
{"contradicts": true|false, "severity": "low"|"medium"|"high", "reason": "<one short clause>"}

If they do not contradict, return {"contradicts": false, "severity": "low", "reason": ""}.
No markdown, no commentary."""


# ---------------------------------------------------------------------------
# Verdict cache
# ---------------------------------------------------------------------------

_VERDICT_DDL = """
CREATE TABLE IF NOT EXISTS contradiction_verdicts (
    claim_a_id INTEGER NOT NULL,
    claim_b_id INTEGER NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    contradicts INTEGER NOT NULL,
    severity TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (claim_a_id, claim_b_id, model, prompt_version)
)
""".strip()


def _connect_verdict_cache(db_path: str) -> sqlite3.Connection:
    """Open the verdict-cache DB with the shared-writer pragmas.

    WAL + busy_timeout are mandatory for every SQLite writer in this codebase
    (the steward, the recall hook, sync, etc. all touch the same file). Without
    them a concurrent writer turns the verdict INSERT into ``database is locked``,
    which re-pays the LLM cost and (per-claim path) counts as a probe error toward
    the circuit breaker. open_conn supplies the uniform envelope; keep the
    historical 30 s grace window for verdict INSERTs racing the steward.
    """
    return open_conn(db_path, busy_ms=30000)


def _canonical_pair(a_id: int, b_id: int) -> tuple[int, int]:
    """Order a pair so the symmetric (a,b)/(b,a) cache to one row."""
    return (a_id, b_id) if a_id <= b_id else (b_id, a_id)


_ensured_verdict_dbs: set[str] = set()


def _ensure_verdict_table(conn: sqlite3.Connection, *, db_key: str | None = None) -> None:
    """Create the verdict cache table if absent.

    ``db_key`` lets per-claim callers skip the redundant ``CREATE TABLE IF NOT
    EXISTS`` + ``commit`` on every claim in a steward cycle: once a given DB has
    been ensured in this process we never re-issue the DDL. ``run_probe`` (one
    DDL per whole run) passes no key and always ensures, preserving its semantics.
    """
    if db_key is not None and db_key in _ensured_verdict_dbs:
        return
    conn.execute(_VERDICT_DDL)
    conn.commit()
    if db_key is not None:
        _ensured_verdict_dbs.add(db_key)


def _cache_get(conn: sqlite3.Connection, a_id: int, b_id: int, model: str) -> dict | None:
    lo, hi = _canonical_pair(a_id, b_id)
    row = conn.execute(
        """SELECT contradicts, severity, reason FROM contradiction_verdicts
           WHERE claim_a_id = ? AND claim_b_id = ? AND model = ? AND prompt_version = ?""",
        (lo, hi, model, PROMPT_VERSION),
    ).fetchone()
    if row is None:
        return None
    return {"contradicts": bool(row[0]), "severity": row[1] or "low", "reason": row[2] or "", "cached": True}


def _cache_put(conn: sqlite3.Connection, a_id: int, b_id: int, model: str, verdict: dict) -> None:
    lo, hi = _canonical_pair(a_id, b_id)
    conn.execute(
        """INSERT OR REPLACE INTO contradiction_verdicts
           (claim_a_id, claim_b_id, model, prompt_version, contradicts, severity, reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (lo, hi, model, PROMPT_VERSION, int(bool(verdict.get("contradicts"))),
         verdict.get("severity", "low"), verdict.get("reason", ""),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Pair sampling
# ---------------------------------------------------------------------------


def _embed_text(claim: Claim) -> str:
    if claim.subject and claim.predicate:
        return f"{claim.subject} {claim.predicate} {claim.object_value or ''} {claim.text}"
    return claim.text


def _same_subject_predicate(a: Claim, b: Claim) -> bool:
    return (
        bool(a.subject) and bool(a.predicate)
        and (a.subject or "").strip().lower() == (b.subject or "").strip().lower()
        and (a.predicate or "").strip().lower() == (b.predicate or "").strip().lower()
    )


def _already_linked(a: Claim, b: Claim) -> bool:
    """Pair is already resolved by supersession — the deterministic path owns it."""
    return (
        a.supersedes_claim_id == b.id or b.supersedes_claim_id == a.id
        or a.replaced_by_claim_id == b.id or b.replaced_by_claim_id == a.id
    )


def _prefiltered(a: Claim, b: Claim) -> bool:
    """Cheap skip BEFORE the LLM: deterministic-domain or already-resolved pairs."""
    if a.status in _SKIP_STATUSES or b.status in _SKIP_STATUSES:
        return True
    if _same_subject_predicate(a, b):
        return True  # conflict_resolver / find_conflicts already own these
    if _already_linked(a, b):
        return True
    return False


def sample_candidate_pairs(
    claims: list[Claim],
    provider: EmbeddingProvider,
    *,
    sim_low: float = 0.60,
    sim_high: float = 0.92,
    limit: int | None = None,
) -> list[tuple[Claim, Claim, float]]:
    """Return (a, b, similarity) pairs in the [sim_low, sim_high) band.

    The band is the key idea: below ``sim_low`` the claims are unrelated (can't
    contradict); at/above ``sim_high`` they're near-duplicates (dedup's job).
    In between is where genuine contradictions live.
    """
    usable = [c for c in claims if c.status not in _SKIP_STATUSES]
    if len(usable) < 2:
        return []
    embeddings = [provider.embed(_embed_text(c)) for c in usable]
    pairs: list[tuple[Claim, Claim, float]] = []
    # The cosine sweep is O(n^2). Once we have already collected ``limit`` in-band
    # pairs there is no point paying for the rest of the quadratic scan — the
    # caller only ever consumes the first ``limit`` after the sort below, so break
    # early instead of building (then discarding) thousands of extra pairs.
    done = False
    for i in range(len(usable)):
        if done:
            break
        for j in range(i + 1, len(usable)):
            if _prefiltered(usable[i], usable[j]):
                continue
            sim = cosine_similarity(embeddings[i], embeddings[j])
            if sim_low <= sim < sim_high:
                pairs.append((usable[i], usable[j], round(sim, 4)))
                if limit is not None and len(pairs) >= limit:
                    done = True
                    break
    pairs.sort(key=lambda p: -p[2])
    if limit is not None:
        pairs = pairs[:limit]
    return pairs


# ---------------------------------------------------------------------------
# LLM judge
# ---------------------------------------------------------------------------


def _judge_llm(a: Claim, b: Claim) -> dict | None:
    """Ask the LLM whether a and b contradict. Returns a verdict dict or None
    on parse/empty failure. May raise LLMBudgetExceeded."""
    body = f"Claim A: {a.text}\nClaim B: {b.text}"
    raw = llm_provider.call_llm(_PROMPT, body)
    if not raw or not raw.strip():
        return None
    for item in llm_provider.parse_json_response(raw):
        if isinstance(item, dict) and "contradicts" in item:
            return {
                "contradicts": bool(item.get("contradicts")),
                # Floor empty/off-vocabulary severity to "medium" at the source
                # (the JSON contract is advisory). Defaulting to "low" here would
                # silently bury a real contradiction in the least-actionable tier
                # and made the "medium" default downstream dead code.
                "severity": _coerce_severity(item.get("severity")),
                "reason": (item.get("reason") or "").strip(),
                "cached": False,
            }
    return None


def _model_key() -> str:
    provider = os.environ.get("MEMORYMASTER_LLM_PROVIDER", "google").strip().lower()
    model = os.environ.get("MEMORYMASTER_LLM_MODEL", "").strip() or "default"
    return f"{provider}:{model}"


_VALID_SEVERITIES = {"low", "medium", "high"}


def _coerce_severity(value: Any) -> str:
    """Normalize a verdict severity to a canonical bucket.

    The LLM judge and verdict cache can both yield an empty or off-vocabulary
    severity (the JSON contract is advisory, not enforced). When that happens we
    floor to ``"medium"`` rather than ``"low"`` so a contradiction surfaced by
    the probe is never silently downgraded to the least-actionable tier.
    """
    sev = str(value or "").strip().lower()
    return sev if sev in _VALID_SEVERITIES else "medium"


def _safe_judge_reason(reason: Any) -> str | None:
    """Return the raw judge reason unless it leaks sensitive content.

    Both ``probe_for_claim`` and ``run_probe(apply=True)`` persist/surface the
    LLM judge reason. Routing both through this guard ensures a reason that the
    sensitivity filter flags is dropped (returns ``None``) on EVERY path — the
    events table must never receive an unredacted secret.
    """
    text = str(reason or "")
    _, leaks = redact_text(text)
    if leaks:
        return None
    return text


# ---------------------------------------------------------------------------
# Wilson confidence interval
# ---------------------------------------------------------------------------


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% CI for a binomial proportion. Returns (low, high).

    Used for the contradiction rate so a handful of judged pairs doesn't read
    as a precise number. ``n`` includes judge errors (counted as non-success).
    """
    if n <= 0:
        return (0.0, 0.0)
    phat = successes / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_probe(
    db_path: str,
    service: Any,
    *,
    limit: int | None = 200,
    sample: int | None = 50,
    sim_low: float = 0.60,
    sim_high: float = 0.92,
    apply: bool = False,
    provider: EmbeddingProvider | None = None,
) -> dict[str, Any]:
    """Sample similar claim pairs, judge contradictions (cached + budget-capped),
    and report a Wilson-bounded contradiction rate.

    Args:
        limit: max claims to load for pair sampling (oldest-first cap upstream).
        sample: max candidate pairs to judge this run.
        apply: if True, flag the lower-confidence claim of each contradicting
            pair as ``conflicted`` (reversible; never archives/supersedes).
    """
    if "://" in str(db_path):
        raise ValueError("contradiction probe is SQLite-only")

    stats: dict[str, Any] = {
        "claims_scanned": 0,
        "candidate_pairs": 0,
        "judged": 0,
        "cache_hits": 0,
        "llm_calls": 0,
        "judge_errors": 0,
        "contradictions": 0,
        "flagged_conflicted": 0,
        "aborted_reason": None,
        "rate": 0.0,
        "rate_ci": [0.0, 0.0],
        "found": [],
    }

    claims = service.store.list_claims(limit=limit or 1000, include_citations=False)
    stats["claims_scanned"] = len(claims)
    prov = provider or create_best_provider()
    pairs = sample_candidate_pairs(claims, prov, sim_low=sim_low, sim_high=sim_high, limit=sample)
    stats["candidate_pairs"] = len(pairs)
    if not pairs:
        return stats

    model = _model_key()
    conn = _connect_verdict_cache(db_path)
    try:
        _ensure_verdict_table(conn)
        with llm_budget.cycle_scope() as budget:
            for a, b, sim in pairs:
                verdict = _cache_get(conn, a.id, b.id, model)
                if verdict is not None:
                    stats["cache_hits"] += 1
                else:
                    try:
                        verdict = _judge_llm(a, b)
                    except llm_budget.LLMBudgetExceeded as exc:
                        stats["aborted_reason"] = exc.reason
                        break
                    stats["llm_calls"] += 1
                    if verdict is None:
                        stats["judge_errors"] += 1
                        stats["judged"] += 1
                        continue
                    _cache_put(conn, a.id, b.id, model, verdict)

                stats["judged"] += 1
                if verdict["contradicts"]:
                    stats["contradictions"] += 1
                    loser, winner = (a, b) if a.confidence <= b.confidence else (b, a)
                    # Same sensitivity guard the per-claim path uses: never let an
                    # unredacted judge reason reach the events table on apply OR the
                    # returned ``found`` report payload (also human/wiki-surfaced).
                    safe_reason = _safe_judge_reason(verdict.get("reason"))
                    stats["found"].append({
                        "claim_a_id": a.id, "claim_b_id": b.id, "similarity": sim,
                        "severity": verdict["severity"], "reason": safe_reason or "",
                        "flag_candidate_id": loser.id,
                    })
                    if apply:
                        detail = f" ({safe_reason})" if safe_reason else ""
                        transition_claim(
                            service.store, loser.id, "conflicted",
                            reason=f"contradiction_probe: contradicts claim {winner.id}{detail}",
                            event_type="transition",
                        )
                        stats["flagged_conflicted"] += 1
            if budget.aborted_reason and not stats["aborted_reason"]:
                stats["aborted_reason"] = budget.aborted_reason
    finally:
        conn.close()

    n = stats["judged"]
    if n > 0:
        stats["rate"] = round(stats["contradictions"] / n, 4)
        lo, hi = wilson_interval(stats["contradictions"], n)
        stats["rate_ci"] = [round(lo, 4), round(hi, 4)]
    return stats


# ---------------------------------------------------------------------------
# Per-claim steward-phase entry point (v3.23)
# ---------------------------------------------------------------------------


def probe_for_claim(
    service: Any,
    claim: Any,
    *,
    sim_low: float = 0.60,
    sim_high: float = 0.92,
    max_pairs: int = 5,
    peer_limit: int = 20,
) -> dict[str, Any]:
    """Per-claim contradiction probe for the steward cycle.

    Finds topically-similar peers for ``claim`` via the existing hybrid
    retrieval (so we reuse the embedder + ranker + cache), excludes pairs the
    deterministic resolver / supersession owns, judges remaining candidates
    against the verdict cache + LLM, and returns a normalized result dict the
    steward wraps in a ``ProbeResult``.

    Returns ``{passed, reasons: [...], metrics: {...}}``. ``passed`` is False
    iff at least one contradiction was found.
    """
    started = time.monotonic()
    metrics: dict[str, Any] = {
        "pairs_checked": 0, "contradictions": 0,
        "cache_hits": 0, "llm_calls": 0, "errors": 0,
        "timed_out": False, "budget_exhausted": False, "duration_ms": 0.0,
    }
    reasons: list[dict[str, Any]] = []

    def _done(passed: bool) -> dict[str, Any]:
        metrics["duration_ms"] = round((time.monotonic() - started) * 1000.0, 3)
        return {"passed": passed, "reasons": reasons, "metrics": metrics}

    if claim.status in _SKIP_STATUSES or not (claim.text or "").strip():
        return _done(True)

    try:
        peers_rows = service.query_rows(
            query_text=claim.text or "",
            limit=peer_limit,
            retrieval_mode="hybrid",
            include_candidates=True,
            include_stale=True,
            include_conflicted=True,
        )
    except Exception as exc:
        reasons.append({
            "code": "contradiction_probe.peer_fetch.error",
            "severity": "low",
            "detail": f"peer fetch failed: {type(exc).__name__}",
            "evidence": {},
        })
        return _done(False)

    candidates: list[tuple[Any, float]] = []
    for row in peers_rows:
        peer = row.get("claim")
        if peer is None or peer.id == claim.id:
            continue
        if peer.status in _SKIP_STATUSES:
            continue
        if _same_subject_predicate(claim, peer):
            continue  # deterministic resolver's domain
        if _already_linked(claim, peer):
            continue
        vs = float(row.get("vector_score") or 0.0)
        # If vectors are present, only accept pairs in the contradiction band.
        # If absent (pure-lexical hybrid), accept all topical peers.
        if vs > 0.0 and not (sim_low <= vs < sim_high):
            continue
        candidates.append((peer, vs))
        if len(candidates) >= max_pairs:
            break

    if not candidates:
        return _done(True)

    db_path = getattr(service.store, "db_path", None)
    if not db_path or "://" in str(db_path):
        return _done(True)  # verdict cache requires SQLite; skip silently on PG

    conn = _connect_verdict_cache(str(db_path))
    try:
        _ensure_verdict_table(conn, db_key=str(db_path))
        model = _model_key()
        for peer, sim in candidates:
            metrics["pairs_checked"] += 1
            verdict = _cache_get(conn, claim.id, peer.id, model)
            if verdict is not None:
                metrics["cache_hits"] += 1
            else:
                try:
                    verdict = _judge_llm(claim, peer)
                except llm_budget.LLMBudgetExceeded:
                    # Budget exhaustion is an expected guardrail, NOT a probe
                    # failure — the steward must not count it toward the circuit
                    # breaker (which would disable the probe for the rest of the
                    # cycle). Flag it distinctly from a genuine timeout.
                    metrics["budget_exhausted"] = True
                    break
                metrics["llm_calls"] += 1
                if verdict is None:
                    metrics["errors"] += 1
                    continue
                _cache_put(conn, claim.id, peer.id, model, verdict)
            if not verdict.get("contradicts"):
                continue
            verdict_reason = _safe_judge_reason(verdict.get("reason"))
            if verdict_reason is None:
                continue  # drop rather than surface a sensitive judge-reason
            metrics["contradictions"] += 1
            reasons.append({
                "code": "contradiction_probe.semantic_pair",
                "severity": _coerce_severity(verdict.get("severity")),
                "detail": f"Semantic contradiction with claim {peer.id}: {verdict_reason}",
                "evidence": {
                    "peer_claim_id": int(peer.id),
                    "similarity": round(float(sim), 4),
                    "from_cache": bool(verdict.get("cached")),
                },
            })
    finally:
        conn.close()
    return _done(metrics["contradictions"] == 0)
