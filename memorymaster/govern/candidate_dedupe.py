"""Pre-steward candidate dedupe (v3.13).

Checks each `candidate` claim against existing claims in the same scope BEFORE
invoking the steward LLM. Candidates that score above a configurable Jaccard
threshold are flagged for archive; the steward applies the SQL transition and
skips its LLM call.

Two-stage:
1. FTS5 OR-query narrows candidates by lexical overlap (cheap top-K filter).
2. Token-set Jaccard scores the final match (corpus-independent, predictable).

We use Jaccard rather than raw BM25 because BM25 in SQLite FTS5 collapses to
near-zero on small corpora (IDF goes to ~0 when most tokens appear in every
doc). Jaccard works the same on a 2-doc fixture and on a 20k-claim DB.

Env flags:
  MEMORYMASTER_DEDUPE_ENABLED      default "0" (off)
  MEMORYMASTER_DEDUPE_SHADOW       default "1" (count would-archive but don't act)
  MEMORYMASTER_DEDUPE_JACCARD_HIGH default "0.85"
  MEMORYMASTER_JEV_DEDUP_PER_CYCLE default "200" (S4 pairs asked per run of the
                                   steward-cycle hook; 0 disables)

S4 DEDUP (4.9.0, ``run_jev``): the same kind of FTS pairs are asked to Jev
(``memory.same_fact``, ``memory.contradicts``, ``memory.supersedes`` in two
paraphrases, ``memory.same_scope``) when ``MEMORYMASTER_JEV_DEDUP`` /
``MEMORYMASTER_JEV_MODE`` is not ``off``, independently of the legacy flags.
Only the scheduled steward-cycle hook calls ``run_jev``, after ``run_cycle``:
``run`` (the stage inside ``MemoryService.run_cycle``, which MCP, the CLI, the
per-turn operator cycle and the scheduler all call) is 4.8.9's and never asks
Jev, whatever the mode. The live action is only a steward proposal tagged
``source: jev`` -- never a status change. Its details are
``steward_proposal:jev_<decision>`` so recall's pending-supersession demotion
and the validator's promotion block (which read
``steward_proposal:superseded_candidate``) ignore it until an operator
approves; ``curation_drain`` never approves it (F-21).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

DedupeAction = Literal["archive", "passthrough"]


@dataclass(frozen=True)
class DedupeResult:
    action: DedupeAction
    canonical_claim_id: int | None
    jaccard_score: float | None
    reason: str


_DEFAULT_JACCARD_HIGH = 0.85
_TRUTHY = {"1", "true", "yes", "on", "y"}
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")
_FTS_TOP_K = 5


def is_enabled() -> bool:
    return os.getenv("MEMORYMASTER_DEDUPE_ENABLED", "0").strip().lower() in _TRUTHY


def is_shadow_mode() -> bool:
    return os.getenv("MEMORYMASTER_DEDUPE_SHADOW", "1").strip().lower() in _TRUTHY


def jaccard_high_threshold() -> float:
    raw = os.getenv("MEMORYMASTER_DEDUPE_JACCARD_HIGH", str(_DEFAULT_JACCARD_HIGH))
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_JACCARD_HIGH


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def jaccard_tokens(a: str, b: str) -> float:
    """Return |A ∩ B| / |A ∪ B| over case-folded word tokens."""
    set_a = _tokenize(a)
    set_b = _tokenize(b)
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union else 0.0


def _escape_fts5_query(text: str) -> str:
    """Build an OR-joined FTS5 query so matches don't require every token."""
    tokens = _TOKEN_RE.findall(text.lower())
    if not tokens:
        return '""'
    escaped = ['"' + token.replace('"', '""') + '"' for token in tokens]
    return " OR ".join(escaped)


def _has_fts5_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims_fts'"
    ).fetchone()
    return row is not None


# Statuses a claim may have and still be a valid "canonical" target to dedupe
# a fresh candidate against. A retired (archived), replaced (superseded), or
# contested (conflicted) claim must NOT be treated as the canonical survivor —
# archiving a newer candidate in favour of one of those drops live information
# in favour of a dead/contested row.
_CANONICAL_DEDUPE_STATUSES = ("confirmed", "candidate", "stale")


def fts_candidates_in_scope(
    conn: sqlite3.Connection,
    *,
    scope: str,
    text: str,
    exclude_id: int,
    limit: int = _FTS_TOP_K,
) -> list[tuple[int, str, str]]:
    """Return list of (id, text, status) candidate matches via FTS5 OR-query.

    Empty list if FTS5 isn't present, scope is empty, or there are no matches.
    Excludes the candidate itself and any claim whose status is not a valid
    canonical-dedupe target (archived / superseded / conflicted) — MED audit
    fix: a fresh candidate must never be archived as a duplicate of a retired,
    replaced, or contested claim, which would drop the possibly-newer candidate
    in favour of a dead one.
    """
    if not text or not text.strip() or not scope:
        return []
    if not _has_fts5_table(conn):
        return []

    fts_query = _escape_fts5_query(text)
    status_placeholders = ", ".join("?" for _ in _CANONICAL_DEDUPE_STATUSES)
    rows = conn.execute(
        f"""
        SELECT c.id, c.text, c.status
        FROM claims c
        JOIN claims_fts ON claims_fts.rowid = c.id
        WHERE claims_fts MATCH ?
          AND c.scope = ?
          AND c.id <> ?
          AND c.status IN ({status_placeholders})
        ORDER BY bm25(claims_fts) ASC
        LIMIT ?
        """,
        (fts_query, scope, exclude_id, *_CANONICAL_DEDUPE_STATUSES, limit),
    ).fetchall()

    return [(int(r[0]), r[1] or "", r[2] or "") for r in rows]


def find_near_duplicate(
    conn: sqlite3.Connection,
    *,
    candidate_id: int,
    candidate_text: str,
    candidate_scope: str,
    jaccard_high: float | None = None,
) -> DedupeResult:
    """Decide whether a candidate is a near-duplicate of an existing claim.

    Pulls top-K BM25 matches in the same scope, then scores each with token
    Jaccard. Returns archive action with the best canonical match if its
    Jaccard score is >= jaccard_high; otherwise passthrough.

    Caller honors shadow mode — this function never mutates state.
    """
    threshold = jaccard_high if jaccard_high is not None else jaccard_high_threshold()

    if not candidate_text or len(candidate_text.strip()) < 10:
        return DedupeResult(
            action="passthrough",
            canonical_claim_id=None,
            jaccard_score=None,
            reason="text-too-short",
        )

    matches = fts_candidates_in_scope(
        conn,
        scope=candidate_scope,
        text=candidate_text,
        exclude_id=candidate_id,
        limit=_FTS_TOP_K,
    )
    if not matches:
        return DedupeResult(
            action="passthrough",
            canonical_claim_id=None,
            jaccard_score=None,
            reason="no-fts-matches",
        )

    best_id: int | None = None
    best_score = 0.0
    best_status = ""
    for cid, ctext, cstatus in matches:
        score = jaccard_tokens(candidate_text, ctext)
        if score > best_score:
            best_score = score
            best_id = cid
            best_status = cstatus

    if best_score >= threshold and best_id is not None:
        return DedupeResult(
            action="archive",
            canonical_claim_id=best_id,
            jaccard_score=best_score,
            reason=f"jaccard>={threshold:.2f} canonical-status={best_status}",
        )

    return DedupeResult(
        action="passthrough",
        canonical_claim_id=best_id,
        jaccard_score=best_score,
        reason=f"jaccard<{threshold:.2f}",
    )


def run(store, *, limit: int = 200) -> dict[str, object]:
    """Pre-validator candidate dedupe stage for MemoryService.run_cycle.

    Scans status='candidate' claims, finds near-duplicates of existing
    same-scope claims via FTS5+Jaccard, and either archives them (active
    mode) or counts them (shadow mode). Returns a stats dict that
    run_cycle merges into its result under the 'dedupe' key.

    No-op when MEMORYMASTER_DEDUPE_ENABLED != "1".
    """
    if not is_enabled():
        return {
            "enabled": False,
            "shadow": False,
            "archived": 0,
            "would_archive": 0,
            "passthrough": 0,
            "avg_jaccard": None,
            "results": [],
        }

    shadow = is_shadow_mode()
    threshold = jaccard_high_threshold()

    candidates = store.find_by_status("candidate", limit=limit)
    archived = 0
    would_archive = 0
    passthrough = 0
    score_sum = 0.0
    score_count = 0
    results: list[dict[str, object]] = []

    with store.connect() as conn:
        for claim in candidates:
            if not claim.text or not claim.scope:
                passthrough += 1
                continue
            decision = find_near_duplicate(
                conn,
                candidate_id=claim.id,
                candidate_text=claim.text,
                candidate_scope=claim.scope,
                jaccard_high=threshold,
            )
            if decision.jaccard_score is not None:
                score_sum += decision.jaccard_score
                score_count += 1
            if decision.action != "archive":
                passthrough += 1
                continue

            results.append({
                "claim_id": claim.id,
                "canonical_id": decision.canonical_claim_id,
                "score": decision.jaccard_score,
                "reason": decision.reason,
                "would_archive": shadow,
            })

            if shadow:
                would_archive += 1
                continue

            conn.execute(
                "UPDATE claims SET status = 'archived', "
                "replaced_by_claim_id = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (decision.canonical_claim_id, claim.id),
            )
            conn.execute(
                "UPDATE claims SET access_count = COALESCE(access_count, 0) + 1, "
                "updated_at = datetime('now') WHERE id = ?",
                (decision.canonical_claim_id,),
            )
            conn.execute(
                "INSERT INTO events (claim_id, event_type, details, created_at) "
                "VALUES (?, 'transition', ?, datetime('now'))",
                (claim.id, f"dedupe-archived: {decision.reason}"),
            )
            archived += 1
        conn.commit()

    avg_jaccard = score_sum / score_count if score_count > 0 else None
    return {
        "enabled": True,
        "shadow": shadow,
        "archived": archived,
        "would_archive": would_archive,
        "passthrough": passthrough,
        "avg_jaccard": avg_jaccard,
        "results": results,
    }


# ----------------------------------------------------------------- S4 DEDUP ---

_log = logging.getLogger(__name__)

JEV_SURFACE = "dedup"
JEV_PAIRS_PER_CYCLE_ENV = "MEMORYMASTER_JEV_DEDUP_PER_CYCLE"
_DEFAULT_JEV_PAIRS_PER_CYCLE = 200
# Pairs with less lexical overlap than this are not worth a request.
_JEV_MIN_JACCARD = 0.2
_JEV_LIVE_STATUSES = ("candidate", "confirmed", "stale")
NO_PROPOSAL = "no_proposal"
PROPOSE_DUPLICATE = "propose_duplicate"
PROPOSE_SUPERSEDE = "propose_supersede"
PROPOSE_CONFLICT = "propose_conflict"
_PROPOSAL_ACTIONS = frozenset({PROPOSE_DUPLICATE, PROPOSE_SUPERSEDE, PROPOSE_CONFLICT})


def jev_pairs_per_cycle() -> int:
    raw = os.getenv(JEV_PAIRS_PER_CYCLE_ENV, "").strip()
    try:
        value = int(raw) if raw else _DEFAULT_JEV_PAIRS_PER_CYCLE
    except ValueError:
        return _DEFAULT_JEV_PAIRS_PER_CYCLE
    return value if value >= 0 else _DEFAULT_JEV_PAIRS_PER_CYCLE


@dataclass(frozen=True)
class _JevPair:
    """memory_a = the existing (FTS-matched) claim, memory_b = the candidate."""

    a_id: int
    a_text: str
    b_id: int
    b_text: str
    scope: str
    tenant: str | None
    jaccard: float
    fts_rank: int

    @property
    def ref(self) -> str:
        return f"pair:{self.a_id}-{self.b_id}"

    @property
    def texts_sha256(self) -> str:
        return hashlib.sha256(f"{self.a_text}\x00{self.b_text}".encode("utf-8")).hexdigest()


def _jev_sensitive(claim: Any) -> bool:
    """Recall's egress policy: a claim the sensitivity scan flags is never sent (fails closed)."""
    try:
        from memorymaster.core.security import is_sensitive_claim

        return bool(is_sensitive_claim(claim))
    except Exception:  # noqa: BLE001 - an unscannable claim is treated as sensitive
        return True


def _jev_pairs(store, candidates) -> list[_JevPair]:
    """Authorized FTS pairs: same scope, same tenant, both public and not sensitive, enough overlap."""
    from types import SimpleNamespace

    pairs: list[_JevPair] = []
    seen: set[frozenset[int]] = set()
    with contextlib.closing(store.connect()) as conn:
        for claim in candidates:
            text = (claim.text or "").strip()
            if (claim.status != "candidate" or len(text) < 10 or not claim.scope
                    or (claim.visibility or "public") != "public" or _jev_sensitive(claim)):
                continue
            matches = fts_candidates_in_scope(conn, scope=claim.scope, text=text, exclude_id=claim.id)
            if not matches:
                continue
            ids = [cid for cid, _text, _status in matches]
            rows = conn.execute(
                f"SELECT id, tenant_id, COALESCE(visibility, 'public'), subject, predicate, object_value "
                f"FROM claims WHERE id IN ({', '.join('?' for _ in ids)})",
                ids,
            ).fetchall()
            meta = {int(row[0]): tuple(row[1:]) for row in rows}
            for rank, (other_id, other_text, _status) in enumerate(matches, start=1):
                tenant, visibility, subject, predicate, object_value = meta.get(
                    other_id, (None, "missing", None, None, None))
                key = frozenset((other_id, claim.id))
                score = jaccard_tokens(text, other_text)
                if (key in seen or visibility != "public" or tenant != claim.tenant_id
                        or score < _JEV_MIN_JACCARD):
                    continue
                if _jev_sensitive(SimpleNamespace(text=other_text, subject=subject, predicate=predicate,
                                                  object_value=object_value)):
                    continue
                seen.add(key)
                pairs.append(_JevPair(other_id, other_text, claim.id, text, claim.scope, claim.tenant_id,
                                      score, rank))
    return pairs


def _jev_answered_pairs(ledger, pairs: list[_JevPair], *, live_only: bool) -> set[frozenset[int]]:
    """Pairs (either order) already answered, or egress-blocked, for the same texts.

    In live mode only live answers count: a pair judged in shadow is asked again.
    """
    refs = sorted({ref for pair in pairs for ref in (pair.ref, f"pair:{pair.b_id}-{pair.a_id}")})
    answered: set[tuple[str, str]] = set()
    for start in range(0, len(refs), 400):
        chunk = refs[start:start + 400]
        rows = ledger.query(
            "SELECT i.item_ref AS ref, d.baseline_features_json AS features "
            "FROM decisions d JOIN decision_items i USING (decision_id) "
            "WHERE d.surface = ? AND (d.transport_outcome = 'ok' OR d.fallback_reason = 'egress_blocked') "
            + ("AND d.mode = 'live' " if live_only else "")
            + f"AND i.item_ref IN ({', '.join('?' for _ in chunk)}) GROUP BY d.decision_id, i.item_ref",
            [JEV_SURFACE, *chunk],
        )
        for row in rows:
            try:
                features = json.loads(row["features"] or "{}")
            except ValueError:
                continue
            if isinstance(features, dict):
                answered.add((row["ref"], str(features.get("texts_sha256"))))
    done: set[frozenset[int]] = set()
    for pair in pairs:
        if (pair.ref, pair.texts_sha256) in answered:
            done.add(frozenset((pair.a_id, pair.b_id)))
        swapped = hashlib.sha256(f"{pair.b_text}\x00{pair.a_text}".encode("utf-8")).hexdigest()
        if (f"pair:{pair.b_id}-{pair.a_id}", swapped) in answered:
            done.add(frozenset((pair.a_id, pair.b_id)))
    return done


def _jev_dedup_choose(ref: str):
    from memorymaster.decisions.engine import JevChoice

    def choose(answers) -> JevChoice:
        def threshold(question_id: str) -> float:
            return answers.threshold(question_id, "propose", 0.8)

        same = answers.get("memory.same_fact", ref)
        p_same = same.probabilities.get("3") if same is not None and same.primitive == "score" else None
        scope = answers.noul("memory.same_scope", ref)
        contradicts = answers.noul("memory.contradicts", ref)
        supersedes = answers.noul("memory.supersedes", ref)
        supersedes_alt = answers.noul("memory.supersedes_alt", ref)
        if None in (p_same, scope, contradicts, supersedes, supersedes_alt):
            return JevChoice(action=NO_PROPOSAL)
        if scope < threshold("memory.same_scope"):
            return JevChoice(action=NO_PROPOSAL)
        if p_same >= threshold("memory.same_fact"):
            return JevChoice(action=PROPOSE_DUPLICATE)
        if supersedes >= threshold("memory.supersedes") and supersedes_alt >= threshold("memory.supersedes_alt"):
            return JevChoice(action=PROPOSE_SUPERSEDE)
        if contradicts >= threshold("memory.contradicts"):
            return JevChoice(action=PROPOSE_CONFLICT)
        return JevChoice(action=NO_PROPOSAL)

    return choose


def _jev_ask(engine, pair: _JevPair):
    from memorymaster.decisions.engine import DecisionContext, DecisionItem
    from memorymaster.decisions.questions import build_dedup

    state, bound = build_dedup(pair.a_text, pair.b_text, scope_label=pair.scope, item_ref=pair.ref)
    items = [DecisionItem(pair.ref, kind="pair"), DecisionItem(f"claim:{pair.a_id}", kind="claim"),
             DecisionItem(f"claim:{pair.b_id}", kind="claim")]
    context = DecisionContext(
        kind="batch", scope=pair.scope, tenant=pair.tenant, exploration="none",
        baseline_features={"jaccard": round(pair.jaccard, 4), "fts_rank": pair.fts_rank,
                           "texts_sha256": pair.texts_sha256},
    )
    return engine.decide(JEV_SURFACE, state=state, questions=bound, items=items, legacy_action=NO_PROPOSAL,
                         choose=_jev_dedup_choose(pair.ref), context=context)


def _jev_evidence(pair: _JevPair, answers: Any) -> dict[str, Any]:
    def answer(question_id: str) -> Any:
        parsed = (answers or {}).get(f"{question_id}::{pair.ref}")
        if parsed is None:
            return None
        return dict(parsed.probabilities) if parsed.primitive == "score" else parsed.value

    evidence = {name.split(".", 1)[1]: answer(name) for name in (
        "memory.same_fact", "memory.contradicts", "memory.supersedes", "memory.supersedes_alt",
        "memory.same_scope")}
    evidence.update(jaccard=round(pair.jaccard, 4), fts_rank=pair.fts_rank, pair_ref=pair.ref)
    return evidence


def _jev_write_proposal(store, pair: _JevPair, action: str, decision_id: str, answers: Any) -> str | None:
    """File the operator proposal for a live S4 action; no claim status changes here.

    Returns why nothing was filed, or ``None`` when the proposal was written.
    """
    evidence = _jev_evidence(pair, answers)
    if action == PROPOSE_DUPLICATE:  # the candidate repeats the existing claim, which stays
        target, related, replaced_by = pair.b_id, pair.a_id, pair.a_id
        decision, proposed = "superseded_candidate", "superseded"
        priority = (evidence.get("same_fact") or {}).get("3")
    elif action == PROPOSE_SUPERSEDE:  # the candidate is the current version of the existing claim
        target, related, replaced_by = pair.a_id, pair.b_id, pair.b_id
        decision, proposed = "superseded_candidate", "superseded"
        priority = min(evidence.get("supersedes") or 0.0, evidence.get("supersedes_alt") or 0.0)
    else:  # the candidate contradicts the existing claim
        target, related, replaced_by = pair.b_id, pair.a_id, None
        decision, proposed = "conflicted", "conflicted"
        priority = evidence.get("contradicts")
    target_claim = store.get_claim(target, include_citations=False)
    related_claim = store.get_claim(related, include_citations=False)
    if (target_claim is None or related_claim is None or target_claim.status not in _JEV_LIVE_STATUSES
            or related_claim.status not in _JEV_LIVE_STATUSES):
        return "claim_status_changed"
    for event in store.list_events(claim_id=target, event_type="policy_decision", limit=200):
        try:
            existing = json.loads(event.payload_json or "{}")
        except ValueError:
            continue
        if (isinstance(existing, dict) and existing.get("source") == "jev" and existing.get("decision") == decision
                and existing.get("related_claim_id") == related):
            return "proposal_exists"
    store.record_event(
        claim_id=target, event_type="policy_decision", from_status=target_claim.status, to_status=proposed,
        details=f"steward_proposal:jev_{decision}",
        payload={
            "source": "jev", "proposal_type": "jev_dedup", "decision": decision, "proposed_status": proposed,
            "priority": round(float(priority or 0.0), 4), "apply_requested": False,
            "reasons": [{"code": f"jev_dedup:{action}", "probe_type": "jev", "severity": "info",
                         "detail": f"Jev S4 judged pair {pair.ref} ({action}); operator review required.",
                         "evidence": evidence}],
            "replaced_by_claim_id": replaced_by, "related_claim_id": related, "decision_id": decision_id,
            "pair_ref": pair.ref, "evidence": evidence,
        },
    )
    return None


def run_jev(store, *, limit: int | None = None, engine: Any = None, candidate_limit: int = 2000,
            concurrency: int = 8) -> dict[str, Any]:
    """S4 entrypoint for the steward-cycle hook only; never called by ``run_cycle``.

    Asks about at most ``limit`` pairs (``None``: ``MEMORYMASTER_JEV_DEDUP_PER_CYCLE``)
    of the first ``candidate_limit`` candidates. Never raises.
    """
    return jev_review(store, limit=candidate_limit, engine=engine, max_pairs=limit, concurrency=concurrency)


def jev_review(store, *, candidates=None, limit: int = 200, engine: Any = None, max_pairs: int | None = None,
               concurrency: int = 8) -> dict[str, Any]:
    """S4: ask Jev about the candidates' FTS pairs; live answers become proposals only.

    Pairs already answered for the same texts are not asked again; at most
    ``max_pairs`` (``MEMORYMASTER_JEV_DEDUP_PER_CYCLE``) requests per call. SQLite
    only (PostgreSQL fails closed). Never raises into the steward cycle.
    """
    summary: dict[str, Any] = {"surface": JEV_SURFACE, "mode": "off", "pairs_considered": 0, "asked": 0,
                               "proposals": 0, "would_propose": 0, "apply_failed": 0, "skipped_already_asked": 0,
                               "fallbacks": {}, "stopped": None}
    try:
        return _jev_review(store, summary, candidates=candidates, limit=limit, engine=engine,
                           max_pairs=max_pairs, concurrency=concurrency)
    except Exception as exc:  # noqa: BLE001 - S4 must never break the steward cycle
        _log.warning("S4 dedup review failed: %s", type(exc).__name__)
        summary["stopped"] = "error"
        return summary


def _jev_review(store, summary: dict[str, Any], *, candidates, limit: int, engine: Any, max_pairs: int | None,
                concurrency: int) -> dict[str, Any]:
    from memorymaster.decisions.engine import default_engine
    from memorymaster.decisions.ledger import LedgerReadError
    from memorymaster.govern.jev_batch import STOP_REASONS, Pacer, record_apply_failed, run_paced

    engine = engine or default_engine()
    summary["mode"] = engine.config.mode_for(JEV_SURFACE)
    if summary["mode"] == "off":
        summary["stopped"] = "mode_off"
        return summary
    if getattr(store, "dsn", None):
        summary["stopped"] = "unsupported_store"
        return summary
    cap = jev_pairs_per_cycle() if max_pairs is None else max(int(max_pairs), 0)
    if cap == 0:
        return summary
    if candidates is None:
        candidates = store.find_by_status("candidate", limit=limit)
    pairs = _jev_pairs(store, candidates)
    summary["pairs_considered"] = len(pairs)
    try:
        answered = _jev_answered_pairs(engine.ledger, pairs, live_only=summary["mode"] == "live")
    except LedgerReadError:
        summary["stopped"] = "ledger_unavailable"
        return summary
    fresh = [pair for pair in pairs if frozenset((pair.a_id, pair.b_id)) not in answered]
    summary["skipped_already_asked"] = len(pairs) - len(fresh)

    def handle(pair: _JevPair, decision: Any) -> str | None:
        summary["asked"] += 1
        reason = getattr(decision, "fallback_reason", "engine_error")
        if reason is not None:
            summary["fallbacks"][reason] = summary["fallbacks"].get(reason, 0) + 1
            return reason if reason in STOP_REASONS else None
        if decision.mode != "live":
            summary["would_propose"] += int(decision.jev_action in _PROPOSAL_ACTIONS)
        elif decision.action in _PROPOSAL_ACTIONS:
            failed = _jev_write_proposal(store, pair, decision.action, decision.decision_id, decision.answers)
            if failed is None:
                summary["proposals"] += 1
            else:  # the decision row says propose_*; record that nothing was filed
                summary["apply_failed"] += 1
                record_apply_failed(engine, decision.decision_id, pair.ref, decision.action, failed)
        return None

    summary["stopped"] = run_paced(fresh[:cap], lambda pair: _jev_ask(engine, pair), handle,
                                   concurrency=concurrency, pacer=Pacer(engine.config.rpm_cap))
    return summary
