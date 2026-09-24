"""S1 REVALIDATE: Jev re-confirms stale claims that are still right and useful.

Contract: ``.planning/JEV-LIVE-4.9.0.md`` (surface ``revalidate``).  One request
per stale claim with the registry's S1 questions (``lifecycle.still_valid``,
``lifecycle.durable``, ``lifecycle.useful_future``); the claim's age and last use
are computed here and sent only as named buckets, never as dates.

Live actions (Jev never archives, degrades or deletes):

* ``still_valid`` >= its ``accept`` threshold and ``useful_future`` >= its
  ``accept`` threshold -> ``reconfirm``: ``stale -> confirmed`` through the
  lifecycle helper (event ``validator``, details ``jev_revalidation:<decision_id>``)
  and the confidence set to the validator's promote score -- unless that score is
  below the decay ``stale_threshold`` (decay would re-stale the claim next cycle,
  and the version bump would make S1 ask again every cycle);
* both below their ``low`` thresholds -> ``no_longer_useful``: nothing changes in
  the store; the live decision row in the ledger *is* the judgment that
  ``decisions.archive_gate`` reads before the scheduled archive may act;
* anything else, shadow mode, every fallback and a late answer -> the claim stays
  stale (the legacy action ``keep_stale``).

Selection is SQLite-only (PostgreSQL fails closed): public stale claims of the
service's tenant, oldest ``updated_at`` first, except claims the sensitivity scan
flags (``is_sensitive_claim``, recall's egress policy: never sent, left stale and
counted as ``skipped_sensitive``), observations (the validator
promotes those only through its deterministic observation gate), claims with an
unresolved supersession proposal (the validator's
``promotion_blocked_pending_supersession`` guard, re-checked before the
transition) and claims already answered within ``REASK_AFTER_DAYS`` for the same
claim version and text (both logged as baseline features, so a confidence write
alone does not make a claim new).  In live mode only live answers count, so
switching from shadow re-asks at once.  ``backfill`` walks the whole stale set in
pages of ``limit``.

The job stops cleanly when the engine reports an exhausted budget, an unreadable
ledger, a missing key or an open breaker, when the optional per-run ``max_usd``
would be exceeded, or -- before asking and again before each re-confirmation --
while a failed integrity check freezes promotions (``promotions_frozen``, the
validator's own guard).  Unlike the validator, S1 fails closed when the
pending-supersession scan cannot read the proposal log: it stops with
``supersession_scan_failed`` and re-confirms nothing more in that run.  A live ``reconfirm`` the store does not apply leaves an
``apply_failed`` outcome next to its decision row.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping

logger = logging.getLogger(__name__)

SURFACE = "revalidate"
RECONFIRM = "reconfirm"
NO_LONGER_USEFUL = "no_longer_useful"
KEEP_STALE = "keep_stale"
#: Stop reason while a failed integrity check freezes promotions (``<db>.integrity-failed``).
FROZEN = "promotions_frozen"
#: Stop reason when the pending-supersession scan faults (fail closed).
SCAN_FAILED = "supersession_scan_failed"
PER_CYCLE_ENV = "MEMORYMASTER_JEV_REVALIDATE_PER_CYCLE"
DEFAULT_PER_CYCLE = 500
#: A claim answered for its current state is not asked again for this long.
REASK_AFTER_DAYS = 30
_PAGE_FLOOR = 50
# One page (and one ledger IN list) never exceeds this, whatever the limit: SQLite caps
# bound parameters, and a 41k-claim backfill page once stopped S1 before its first ask.
_PAGE_CAP = 500

_STALE_PAGE_SQL = """
    SELECT id, updated_at, version, text FROM claims
    WHERE status = 'stale' AND COALESCE(visibility, 'public') = 'public' AND tenant_id IS ?
      AND LOWER(TRIM(COALESCE(claim_type, ''))) <> 'observation'
      AND (updated_at > ? OR (updated_at = ? AND id > ?))
    ORDER BY updated_at ASC, id ASC
    LIMIT ?
"""


def per_cycle_limit(environ: Mapping[str, str] | None = None) -> int:
    """Per steward cycle cap: ``MEMORYMASTER_JEV_REVALIDATE_PER_CYCLE`` (0 disables)."""
    raw = (os.environ if environ is None else environ).get(PER_CYCLE_ENV)
    try:
        value = int(str(raw).strip()) if raw is not None and str(raw).strip() else DEFAULT_PER_CYCLE
    except ValueError:
        return DEFAULT_PER_CYCLE
    return value if value >= 0 else DEFAULT_PER_CYCLE


@dataclass(frozen=True)
class _Job:
    claim: Any
    citation_count: int
    age: str
    last_used: str

    @property
    def ref(self) -> str:
        return f"claim:{self.claim.id}"

    @property
    def text_sha256(self) -> str:
        return _sha256(self.claim.text)


def _sha256(text: Any) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _bucket(value: Any, now: datetime, *, missing: str = "unknown") -> str:
    moment = _parse(value)
    if moment is None:
        return missing
    days = (now - moment).total_seconds() / 86400.0
    if days < 7:
        return "younger_than_7_days"
    if days < 30:
        return "7_to_30_days"
    if days < 90:
        return "30_to_90_days"
    return "older_than_90_days"


def _summary(mode: str, backfill: bool) -> dict[str, Any]:
    return {
        "ok": True, "surface": SURFACE, "mode": mode, "backfill": backfill,
        "selected": 0, "asked": 0, "reconfirmed": 0, "no_longer_useful": 0, "kept_stale": 0,
        "would_reconfirm": 0, "would_judge_no_longer_useful": 0, "fallbacks": {},
        "skipped_recently_judged": 0, "skipped_pending_supersession": 0, "blocked_pending_supersession": 0,
        "skipped_sensitive": 0, "blocked_supersession_scan": 0,
        "changed_since_asked": 0, "confirm_failed": 0, "blocked_frozen": 0, "score_below_stale_threshold": 0,
        "spent_usd": 0.0, "stopped": None,
    }


def _choose(ref: str):
    from memorymaster.decisions.engine import JevChoice

    def choose(answers) -> JevChoice:
        valid = answers.noul("lifecycle.still_valid", ref)
        useful = answers.noul("lifecycle.useful_future", ref)
        if valid is None or useful is None:
            return JevChoice(action=KEEP_STALE)
        if (valid >= answers.threshold("lifecycle.still_valid", "accept", 0.85)
                and useful >= answers.threshold("lifecycle.useful_future", "accept", 0.7)):
            return JevChoice(action=RECONFIRM)
        if (valid < answers.threshold("lifecycle.still_valid", "low", 0.2)
                and useful < answers.threshold("lifecycle.useful_future", "low", 0.2)):
            return JevChoice(action=NO_LONGER_USEFUL)
        return JevChoice(action=KEEP_STALE)

    return choose


def _recently_answered(ledger: Any, keys: dict[int, tuple[Any, str]], now: datetime, *,
                       live_only: bool) -> set[int]:
    """Claim ids (of ``{id: (version, text sha256)}``) answered, or egress-blocked, for that state.

    Raises ``LedgerReadError`` when an existing ledger cannot be read.
    """
    if not keys:
        return set()
    refs = [f"claim:{claim_id}" for claim_id in keys]
    rows = ledger.query(
        "SELECT i.item_ref AS ref, d.baseline_features_json AS features "
        "FROM decisions d JOIN decision_items i USING (decision_id) "
        "WHERE d.surface = ? AND d.ts >= ? AND (d.transport_outcome = 'ok' OR d.fallback_reason = 'egress_blocked') "
        + ("AND d.mode = 'live' " if live_only else "")
        + f"AND i.item_ref IN ({', '.join('?' for _ in refs)}) GROUP BY d.decision_id, i.item_ref",
        [SURFACE, (now - timedelta(days=REASK_AFTER_DAYS)).isoformat(timespec="microseconds"), *refs],
    )
    seen: set[tuple[str, Any, Any]] = set()
    for row in rows:
        try:
            features = json.loads(row["features"] or "{}")
        except ValueError:
            continue
        if isinstance(features, dict):
            seen.add((row["ref"], features.get("claim_version"), features.get("text_sha256")))
    return {claim_id for claim_id, (version, sha) in keys.items() if (f"claim:{claim_id}", version, sha) in seen}


def _sensitive(claim: Any) -> bool:
    """Recall's egress policy: a claim the sensitivity scan flags is never sent (fails closed)."""
    try:
        from memorymaster.core.security import is_sensitive_claim

        return bool(is_sensitive_claim(claim))
    except Exception:  # noqa: BLE001 - an unscannable claim is treated as sensitive
        return True


class _ClaimEvents:
    """One claim's ``list_events``: the pending-supersession scan reads only its rows.

    Proposals and their resolutions are both recorded on the target claim; a
    resolution recorded elsewhere would leave the claim pending (fail closed).
    """

    def __init__(self, store: Any, claim_id: int) -> None:
        self._store = store
        self._claim_id = claim_id

    def list_events(self, **kwargs: Any) -> list[Any]:
        return self._store.list_events(claim_id=self._claim_id, **kwargs)


def _pending_supersession(store: Any, claim_id: int | None = None) -> frozenset[int]:
    """Claims someone declared outdated whose proposal is unresolved (the validator's block).

    Uncached, so a proposal filed seconds ago counts; with ``claim_id`` only that
    claim's events are read.  Unlike the validator, a scan fault raises
    ``PendingSupersessionScanError`` (strict): S1 must not promote blind.
    """
    from memorymaster.recall.retrieval import pending_supersession_ids

    source = store if claim_id is None else _ClaimEvents(store, claim_id)
    return pending_supersession_ids(source, use_cache=False, strict=True)


class _Selection:
    """Stale claims oldest first, keyset-paged; tracks why claims were skipped."""

    def __init__(self, service: Any, engine: Any, *, limit: int, backfill: bool, summary: dict[str, Any]) -> None:
        self.store = service.store
        self.tenant = getattr(service, "tenant_id", None)
        self.engine = engine
        self.limit = limit
        self.backfill = backfill
        self.summary = summary
        self.stop_reason: str | None = None

    def _page(self, cursor: tuple[str, int], size: int) -> list[tuple[int, str, Any, str]]:
        with contextlib.closing(self.store.connect()) as conn:
            rows = conn.execute(_STALE_PAGE_SQL, (self.tenant, cursor[0], cursor[0], cursor[1], size)).fetchall()
        return [(int(row[0]), str(row[1] or ""), row[2], _sha256(row[3])) for row in rows]

    def __iter__(self) -> Iterator[_Job]:
        from memorymaster.decisions.ledger import LedgerReadError
        from memorymaster.recall.retrieval import PendingSupersessionScanError

        size = min(max(self.limit, _PAGE_FLOOR) if not self.backfill else max(self.limit, 1), _PAGE_CAP)
        live_only = self.engine.config.mode_for(SURFACE) == "live"
        cursor: tuple[str, int] = ("", 0)
        yielded = 0
        now = datetime.now(timezone.utc)
        outdated: frozenset[int] | None = None
        while self.backfill or yielded < self.limit:
            page = self._page(cursor, size)
            if not page:
                return
            cursor = (page[-1][1], page[-1][0])
            states = {cid: (version, sha) for cid, _, version, sha in page}
            try:  # skip already-answered claims before loading them
                answered = _recently_answered(self.engine.ledger, states, now, live_only=live_only)
            except LedgerReadError:
                self.stop_reason = "ledger_unavailable"
                return
            self.summary["skipped_recently_judged"] += len(answered)
            # Never spend on a claim the validator would refuse to promote: Jev sees
            # only the old text, never the correction that declared it outdated.
            if outdated is None:  # one scan per run; the transition re-checks each claim
                try:
                    outdated = _pending_supersession(self.store)
                except PendingSupersessionScanError:  # fail closed: ask and promote nothing
                    self.stop_reason = SCAN_FAILED
                    return
            pending = outdated & (set(states) - answered)
            self.summary["skipped_pending_supersession"] += len(pending)
            wanted = [cid for cid, *_ in page if cid not in answered and cid not in pending]
            if not self.backfill:
                wanted = wanted[:self.limit - yielded]
            claims = [c for c in (self.store.get_claim(cid, include_citations=False) for cid in wanted)
                      if c is not None and c.status == "stale"]
            sensitive = {c.id for c in claims if _sensitive(c)}
            self.summary["skipped_sensitive"] += len(sensitive)
            claims = [c for c in claims if c.id not in sensitive]
            counts = self.store.count_citations_batch([c.id for c in claims]) if claims else {}
            for claim in claims:
                yielded += 1
                self.summary["selected"] += 1
                yield _Job(claim, int(counts.get(claim.id, 0)), _bucket(claim.created_at, now),
                           _bucket(claim.last_accessed, now, missing="never"))
            if len(page) < size:
                return


def _ask(engine: Any, job: _Job):
    from memorymaster.decisions.engine import DecisionContext, DecisionItem
    from memorymaster.decisions.questions import build_revalidate

    claim = job.claim
    state, bound = build_revalidate(
        {"text": claim.text, "subject": claim.subject, "predicate": claim.predicate,
         "object_value": claim.object_value, "claim_type": claim.claim_type, "scope": claim.scope},
        age_bucket=job.age, access_bucket=job.last_used, item_ref=job.ref,
    )
    context = DecisionContext(
        kind="batch", scope=claim.scope, tenant=claim.tenant_id, exploration="none",
        baseline_features={"confidence": round(float(claim.confidence or 0.0), 4),
                           "citation_count": job.citation_count, "access_count": int(claim.access_count or 0),
                           "age": job.age, "last_used": job.last_used, "claim_type": claim.claim_type,
                           "pinned": bool(claim.pinned), "claim_version": claim.version,
                           "text_sha256": job.text_sha256},
    )
    return engine.decide(SURFACE, state=state, questions=bound, items=[DecisionItem(job.ref, kind="claim")],
                         legacy_action=KEEP_STALE, choose=_choose(job.ref), context=context)


def _reconfirm(store: Any, job: _Job, decision_id: str, summary: dict[str, Any]) -> str | None:
    """Apply a live ``reconfirm``; return why it was not applied, or ``None`` when it was."""
    from memorymaster.core.config import get_config
    from memorymaster.core.lifecycle import transition_claim
    from memorymaster.govern.jobs.integrity import promotions_frozen_for
    from memorymaster.govern.jobs.validator import validation_score
    from memorymaster.recall.retrieval import PendingSupersessionScanError

    if promotions_frozen_for(store):  # a quick_check failed while Jev was answering
        summary["blocked_frozen"] += 1
        return FROZEN
    current = store.get_claim(job.claim.id, include_citations=False)
    if current is None or current.status != "stale" or current.version != job.claim.version:
        summary["changed_since_asked"] += 1  # the answer was about another state of the claim
        return "changed_since_asked"
    try:
        pending = _pending_supersession(store, current.id)
    except PendingSupersessionScanError:  # fail closed: cannot rule out a supersession
        summary["blocked_supersession_scan"] += 1
        return SCAN_FAILED
    if current.id in pending:  # declared outdated while Jev answered
        summary["blocked_pending_supersession"] += 1
        return "pending_supersession"
    score = validation_score(current, job.citation_count, prior_confidence=current.confidence)
    if score < get_config().stale_threshold:  # decay would re-stale it next cycle and S1 would pay again
        summary["score_below_stale_threshold"] += 1
        return "score_below_stale_threshold"
    reason = f"jev_revalidation:{decision_id}"
    try:
        transition_claim(store, current.id, "confirmed", reason=reason, event_type="validator",
                         event_payload={"source": "jev", "decision_id": decision_id, "score": score,
                                        "citation_count": job.citation_count})
    except Exception as exc:  # noqa: BLE001 - e.g. confirmed-tuple conflict, source review, race
        logger.debug("S1 re-confirmation of claim %s skipped: %s", current.id, type(exc).__name__)
        summary["confirm_failed"] += 1
        return "confirm_failed"
    store.set_confidence(current.id, score,
                         details=f"{reason} validator_score={score:.3f};citations={job.citation_count}")
    summary["reconfirmed"] += 1
    return None


def _cost(engine: Any, decision_id: str) -> float:
    try:
        rows = engine.ledger.query("SELECT cost_usd FROM decisions WHERE decision_id = ?", [decision_id])
    except Exception:  # noqa: BLE001 - an unreadable ledger stops the next decision anyway
        return 0.0
    return float(rows[0]["cost_usd"] or 0.0) if rows else 0.0


def tenants_with_work(store: Any, *, status: str = "stale") -> list[str | None]:
    """Tenant ids (``None`` first) holding claims in ``status``: the steward runs its Jev
    stages once per tenant, because a service only sees its own tenant's claims."""
    with contextlib.closing(store.connect()) as conn:
        rows = conn.execute("SELECT DISTINCT tenant_id FROM claims WHERE status = ?", (status,)).fetchall()
    tenants = {row[0] for row in rows}
    return ([None] if None in tenants else []) + sorted(t for t in tenants if t is not None)


def run(service: Any, *, limit: int = DEFAULT_PER_CYCLE, max_usd: float | None = None, backfill: bool = False,
        engine: Any = None, concurrency: int = 8) -> dict[str, Any]:
    """Ask S1 about up to ``limit`` stale claims (all of them with ``backfill``) and act."""
    from memorymaster.decisions.engine import default_engine
    from memorymaster.govern.jev_batch import STOP_REASONS, Pacer, record_apply_failed, run_paced
    from memorymaster.govern.jobs.integrity import promotions_frozen_for

    engine = engine or default_engine()
    mode = engine.config.mode_for(SURFACE)
    summary = _summary(mode, backfill)
    if getattr(service.store, "dsn", None):  # SQLite-only selection: PostgreSQL fails closed
        summary.update(ok=False, stopped="unsupported_store")
        return summary
    if mode == "off":
        summary["stopped"] = "mode_off"
        return summary
    if limit <= 0:
        return summary
    if max_usd is not None and max_usd <= 0:
        summary["stopped"] = "max_usd"
        return summary
    store = service.store
    if promotions_frozen_for(store):  # like the validator: nothing is promoted through a broken btree
        summary["stopped"] = FROZEN
        return summary
    selection = _Selection(service, engine, limit=limit, backfill=backfill, summary=summary)
    scan_failed: list[bool] = []  # once the scan faults, nothing else is re-confirmed in this run

    def ask(job: _Job):
        return _ask(engine, job)

    def handle(job: _Job, decision: Any) -> str | None:
        summary["asked"] += 1
        if decision is None:
            summary["fallbacks"]["engine_error"] = summary["fallbacks"].get("engine_error", 0) + 1
            return None
        summary["spent_usd"] += _cost(engine, decision.decision_id)
        reason = decision.fallback_reason
        if reason is not None:
            summary["fallbacks"][reason] = summary["fallbacks"].get(reason, 0) + 1
            summary["kept_stale"] += 1
            return reason if reason in STOP_REASONS else None
        if decision.mode != "live":
            summary["would_reconfirm"] += int(decision.jev_action == RECONFIRM)
            summary["would_judge_no_longer_useful"] += int(decision.jev_action == NO_LONGER_USEFUL)
            summary["kept_stale"] += 1
        elif decision.action == RECONFIRM:
            if scan_failed:  # an answer still in flight after the stop: never acted on
                summary["blocked_supersession_scan"] += 1
                failed = SCAN_FAILED
            else:
                failed = _reconfirm(store, job, decision.decision_id, summary)
            if failed is not None:
                summary["kept_stale"] += 1
                record_apply_failed(engine, decision.decision_id, job.ref, RECONFIRM, failed)
                if failed == SCAN_FAILED:
                    scan_failed.append(True)
                if failed in (FROZEN, SCAN_FAILED):
                    return failed
        elif decision.action == NO_LONGER_USEFUL:
            summary["no_longer_useful"] += 1  # recorded by the live decision row itself
        else:
            summary["kept_stale"] += 1
        if max_usd is not None:
            average = summary["spent_usd"] / max(summary["asked"], 1)
            if summary["spent_usd"] + average > max_usd:
                return "max_usd"
        return None

    stopped = run_paced(selection, ask, handle, concurrency=concurrency, pacer=Pacer(engine.config.rpm_cap))
    summary["stopped"] = stopped or selection.stop_reason
    summary["spent_usd"] = round(summary["spent_usd"], 8)
    return summary


__all__ = ["DEFAULT_PER_CYCLE", "PER_CYCLE_ENV", "per_cycle_limit", "run"]
