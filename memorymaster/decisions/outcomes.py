"""Outcome joiners: append rewards to the decisions ledger, never touch authoritative data.

(a) :func:`record_turn_usage` — for items exposed by a session's recent RECALL/
    SESSION decisions, ``used_in_turn`` when the claim ``human_id`` or a
    distinctive word 8-gram of its text appears in the assistant text or the
    tool-call inputs of the turn (``label_source=detector``).  The turn's own
    timestamp (``observed_at``) is required so replays are idempotent.
(b) :func:`tail_lifecycle` — events for claims in ``decision_items`` since a
    watermark become outcomes (``steward_confirmed``, ``revalidated``,
    ``archived``, ``superseded``, ``stale``, ``conflicted``,
    ``proposal_approved``/``proposal_rejected``).  Only real status changes
    count: a same-status bookkeeping event (a confidence write) or a proposal
    (``policy_decision``/``action_proposal``) is not an outcome.  ``label_source``
    comes from the event's actor (review F-21): an explicit ``actor`` of
    ``operator`` or ``automation`` wins; a ``steward_automation:`` details prefix
    is ``automation``; a ``steward_human_override:`` prefix with no payload actor
    predates F-21 (automation wrote it too) and is ``unknown_actor`` (ruling R5,
    never ``operator``); a legacy ``human_override`` resolution with neither is
    ``unattributed_override``; neither may be used as human ground truth.  Jev's
    own actions (``jev_revalidation:``) are ``jev`` (never a label for Jev), even
    with an actor.
    A claim created by a Dreaming application links back to its S3 INGEST
    decision through a ``dream_claim_created`` outcome (item ``claim:<id>``),
    so those labels join that decision too; when the linked candidate had been
    held and released, its ``steward_confirmed`` also records
    ``held_later_confirmed`` on the candidate (``dream:<capture>:<candidate>``).
(c) :func:`record_skill_invocation` and (d) :func:`record_held_release`.
(e) :func:`steward_cycle_outcomes` — the steward-cycle wiring: (b) every cycle and
    the ledger retention prune at most once a day.

All joiners are idempotent: outcome rows are unique on
``(decision_id, item_ref, kind, observed_at)`` and progress is watermarked.  The
authoritative database is opened read-only (``mode=ro`` + ``query_only``).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from memorymaster.decisions.ledger import DecisionLedger, LedgerReadError, OutcomeRecord, utc_iso
from memorymaster.stores._storage_shared import connect_ro

LIFECYCLE_WATERMARK = "lifecycle_events"
TURN_SURFACES = ("recall", "session")
TURN_WINDOW = timedelta(hours=12)
SKILL_WINDOW = timedelta(hours=6)
NGRAM = 8
MIN_CONTENT_TOKENS = 4
_TOKEN = re.compile(r"\w+", re.UNICODE)
_STOPWORDS = frozenset(
    "a an the and or but if then else of to in on at by for from with without into onto over under as is are was "
    "were be been being it its this that these those there here we you he she they i me my our your their us them "
    "do does did done have has had not no yes so than too very can could should would will shall may might must "
    "all any some each every what which who whom whose when where why how also just only about after before "
    "el la los las un una y o de del en por para con sin que es son se lo al".split()
)
STEWARD_EVENTS = frozenset({"validator", "deterministic_validator", "policy_decision", "action_proposal",
                            "supersession", "extractor"})
AUTOMATION_EVENTS = frozenset({"decay", "staleness", "compactor", "compaction_run", "dedup", "dedup_run", "system",
                               "confidence"})
_STATUS_KINDS = {"archived": "archived", "superseded": "superseded", "stale": "stale", "conflicted": "conflicted"}
# Proposals carry the status they ask for in ``to_status``; nothing changed yet.
_PROPOSAL_EVENTS = frozenset({"policy_decision", "action_proposal"})
PRUNE_WATERMARK = "prune_job:last_run"
#: Outcome linking a claim created by a Dreaming application (item ``claim:<id>``)
#: to the S3 INGEST decision of its candidate; details carry ``dream_ref`` and ``released``.
CLAIM_LINK_KIND = "dream_claim_created"
PRUNE_INTERVAL = timedelta(days=1)
_ACTORS = frozenset({"operator", "automation"})

ClaimLookup = Callable[[str], "tuple[str | None, str] | None"]


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _lag(later: datetime | None, earlier: datetime | None) -> int | None:
    if later is None or earlier is None:
        return None
    return max(0, int((later - earlier).total_seconds()))


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


# ------------------------------------------------------------------ detector ---

def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text or "")]


def distinctive_ngrams(text: str, n: int = NGRAM) -> set[str]:
    """Word n-grams with at least ``MIN_CONTENT_TOKENS`` non-stopword, non-numeric tokens."""
    tokens = _tokens(text)
    grams: set[str] = set()
    for start in range(0, len(tokens) - n + 1):
        window = tokens[start:start + n]
        content = sum(1 for t in window if t not in _STOPWORDS and not t.isdigit())
        if content >= MIN_CONTENT_TOKENS:
            grams.add(" ".join(window))
    return grams


def _haystack_ngrams(text: str, n: int = NGRAM) -> set[str]:
    tokens = _tokens(text)
    return {" ".join(tokens[i:i + n]) for i in range(0, len(tokens) - n + 1)}


def detect_usage(human_id: str | None, claim_text: str, haystack: str,
                 haystack_grams: set[str] | None = None) -> str | None:
    """Return ``"human_id"``, ``"ngram"`` or ``None``."""
    if human_id:
        pattern = re.compile(rf"(?<![\w-]){re.escape(human_id)}(?![\w-])", re.IGNORECASE)
        if pattern.search(haystack or ""):
            return "human_id"
    grams = distinctive_ngrams(claim_text)
    if grams and grams & (haystack_grams if haystack_grams is not None else _haystack_ngrams(haystack)):
        return "ngram"
    return None


def _turn_text(turn: Mapping[str, Any]) -> str:
    parts = [str(turn.get("assistant_text") or "")]
    for tool_input in turn.get("tool_inputs") or []:
        parts.append(tool_input if isinstance(tool_input, str) else _dumps(tool_input))
    return "\n".join(parts)


def _session_watermark(session_key: str) -> str:
    return "turn_usage:" + hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:24]


def record_turn_usage(ledger: DecisionLedger, session_key: str, transcript_turn: Mapping[str, Any], *,
                      observed_at: str, lookup: ClaimLookup, window: timedelta = TURN_WINDOW,
                      surfaces: Sequence[str] = TURN_SURFACES) -> int:
    """Join one assistant turn to the items exposed in that session's recent decisions.

    ``observed_at`` is the turn's own timestamp from the transcript (required, never
    "now"): it keys the outcome rows, so replaying a turn writes nothing new.
    """
    observed_raw = str(observed_at or "")
    observed = _parse_ts(observed_raw)
    if not session_key or observed is None or not ledger.exists():
        return 0
    mark_name = _session_watermark(session_key)
    previous = _parse_ts(ledger.get_watermark(mark_name))
    if previous is not None and observed <= previous:
        return 0
    placeholders = ", ".join("?" for _ in surfaces)
    try:
        exposures = ledger.query(
            "SELECT d.decision_id, d.ts, i.item_ref FROM decisions d JOIN decision_items i USING (decision_id) "
            f"WHERE d.session_key = ? AND d.surface IN ({placeholders}) AND d.ts >= ? AND d.ts <= ? "
            "AND i.exposed = 1 GROUP BY d.decision_id, i.item_ref",
            [session_key, *surfaces, utc_iso(observed - window), utc_iso(observed)],
        )
    except LedgerReadError:  # keep the watermark: a retry of this turn must not be skipped
        return 0
    haystack = _turn_text(transcript_turn)
    haystack_grams = _haystack_ngrams(haystack)
    cache: dict[str, Any] = {}
    rows: list[OutcomeRecord] = []
    for exposure in exposures:
        ref = exposure["item_ref"]
        if ref not in cache:
            try:
                cache[ref] = lookup(ref)
            except Exception:
                cache[ref] = None
        found = cache[ref]
        if not found:
            continue
        human_id, text = found
        via = detect_usage(human_id, text or "", haystack, haystack_grams)
        if via is None:
            continue
        rows.append(OutcomeRecord(
            exposure["decision_id"], ref, "used_in_turn", 1.0, was_exposed=1, outcome_window="turn",
            reward_version="used_in_turn.v1", label_source="detector", observed_at=observed_raw,
            lag_s=_lag(observed, _parse_ts(exposure["ts"])),
            details_json=_dumps({"turn_id": transcript_turn.get("turn_id"), "via": via}),
        ))
    inserted = ledger.record_outcomes_checked(rows) if rows else 0
    if inserted is None:  # keep the watermark so a retry of this turn is not skipped
        return 0
    ledger.set_watermark(mark_name, observed_raw)
    return inserted


def claim_lookup(db_path: str | Path) -> ClaimLookup:
    """Read-only ``claim:<id>`` -> ``(human_id, text)`` lookup against the authoritative DB."""
    def lookup(item_ref: str) -> tuple[str | None, str] | None:
        if not item_ref.startswith("claim:"):
            return None
        try:
            claim_id = int(item_ref.split(":", 1)[1])
        except ValueError:
            return None
        conn = _connect_ro(db_path)
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT human_id, text FROM claims WHERE id = ?", (claim_id,)).fetchone()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        return (row[0], row[1] or "") if row else None

    return lookup


# ------------------------------------------------------------ lifecycle tail ---

def _connect_ro(db_path: str | Path) -> sqlite3.Connection | None:
    """Physically read-only connection (``mode=ro`` + ``query_only``); never creates a file."""
    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        return connect_ro(path)
    except sqlite3.Error:
        return None


def label_source(event_type: str, details: str | None, payload: Mapping[str, Any] | None) -> str:
    """Actor-aware provenance of a lifecycle event (F-21).

    ``jev_revalidation:`` is Jev's own action (``jev``, never an independent label)
    whatever actor it records.  Otherwise an explicit payload ``actor`` wins, then
    the details prefixes a proposal resolution writes on status changes:
    ``steward_automation:`` is ``automation``.  ``steward_human_override:`` without
    a payload actor is ``unknown_actor`` (ruling R5): since F-21 an operator
    resolution always records its actor, so the bare prefix is a pre-F-21 event,
    when automatic approvals wrote it too.  A legacy ``human_override`` with
    neither is ``unattributed_override``.
    """
    data = payload if isinstance(payload, Mapping) else {}
    text = str(details or "")
    if text.startswith("jev_revalidation:"):
        return "jev"
    actor = data.get("actor")
    if isinstance(actor, str) and actor in _ACTORS:
        return actor
    if actor is not None:
        return "unknown"
    if data.get("source") == "jev":
        return "jev"
    if text.startswith("steward_automation:"):
        return "automation"
    if text.startswith("steward_human_override:"):
        return "unknown_actor"
    if data.get("source") == "human_override":
        return "unattributed_override"
    if event_type in STEWARD_EVENTS:
        return "steward"
    if event_type in AUTOMATION_EVENTS:
        return "automation"
    return "unknown"


def lifecycle_kind(event_type: str, to_status: str | None, details: str | None, *,
                   from_status: str | None = None) -> str | None:
    """Outcome kind of one event; status kinds only for a real status change."""
    text = str(details or "")
    if event_type in _PROPOSAL_EVENTS or (from_status is not None and from_status == to_status):
        to_status = None
    if to_status == "confirmed":
        return "revalidated" if text.startswith("jev_revalidation:") else "steward_confirmed"
    if to_status in _STATUS_KINDS:
        return _STATUS_KINDS[to_status]
    if event_type == "audit" and text == "steward_proposal_approved":
        return "proposal_approved"
    if event_type == "audit" and text == "steward_proposal_rejected":
        return "proposal_rejected"
    return None


def _decision_items_for(ledger: DecisionLedger, refs: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(refs), 500):
        chunk = refs[start:start + 500]
        rows = ledger.query(
            "SELECT i.decision_id, i.item_ref, MAX(i.exposed) AS exposed, d.ts FROM decision_items i "
            f"JOIN decisions d USING (decision_id) WHERE i.item_ref IN ({', '.join('?' for _ in chunk)}) "
            "GROUP BY i.decision_id, i.item_ref",
            chunk,
        )
        for row in rows:
            found.setdefault(row["item_ref"], []).append(row)
        links = ledger.query(
            "SELECT o.decision_id, o.item_ref, 0 AS exposed, d.ts, MIN(o.details_json) AS link FROM outcomes o "
            f"JOIN decisions d USING (decision_id) WHERE o.kind = ? AND o.item_ref IN ({', '.join('?' for _ in chunk)}) "
            "GROUP BY o.decision_id, o.item_ref",
            [CLAIM_LINK_KIND, *chunk],
        )
        for row in links:
            known = {match["decision_id"] for match in found.get(row["item_ref"], [])}
            if row["decision_id"] not in known:
                found.setdefault(row["item_ref"], []).append(row)
    return found


def _released_dream_ref(match: Mapping[str, Any]) -> str | None:
    """The ``dream:`` item of a claim link whose candidate had been held and released."""
    try:
        link = json.loads(match.get("link") or "null")
    except (TypeError, ValueError):
        return None
    if isinstance(link, Mapping) and link.get("released") is True and isinstance(link.get("dream_ref"), str):
        return link["dream_ref"]
    return None


def _initial_watermark(ledger: DecisionLedger, conn: sqlite3.Connection) -> int | None:
    """First run: skip history older than the first decision (indexed on created_at).

    ``None`` means "do nothing now": no decisions yet (watermark set to the latest
    event) or the ledger could not be read (watermark left unset for a retry).
    """
    try:
        if not ledger.query("SELECT 1 AS present FROM decisions LIMIT 1"):
            latest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
            ledger.set_watermark(LIFECYCLE_WATERMARK, str(int(latest)))
            return None
        earliest = _parse_ts(ledger.query("SELECT MIN(ts) AS ts FROM decisions")[0]["ts"])
        if earliest is None:
            return 0
        floor = (earliest - timedelta(seconds=1)).replace(microsecond=0).isoformat()
        first = conn.execute("SELECT MIN(id) FROM events WHERE created_at >= ?", (floor,)).fetchone()[0]
    except LedgerReadError:
        return None
    except (sqlite3.Error, IndexError, KeyError):
        return 0
    if first is None:
        latest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
        return int(latest)
    return int(first) - 1


def tail_lifecycle(ledger: DecisionLedger, db_path: str | Path, *, batch: int = 2000,
                   max_batches: int = 1000) -> int:
    """Append lifecycle outcomes for decided claims since the watermark; return rows inserted."""
    if not ledger.exists():  # no decisions yet: nothing to join, and no ledger file to create
        return 0
    conn = _connect_ro(db_path)
    if conn is None:
        return 0
    inserted = 0
    try:
        stored = ledger.get_watermark(LIFECYCLE_WATERMARK)
        try:
            watermark = int(stored) if stored is not None else None
        except ValueError:  # corrupt: re-derive (outcome rows are idempotent)
            watermark = None
        if watermark is None:
            watermark = _initial_watermark(ledger, conn)
        if watermark is None:
            return 0
        for _ in range(max_batches):
            try:
                events = conn.execute(
                    "SELECT id, claim_id, event_type, from_status, to_status, details, payload_json, created_at "
                    "FROM events "
                    "WHERE id > ? AND claim_id IS NOT NULL ORDER BY id LIMIT ?",
                    (watermark, int(batch)),
                ).fetchall()
            except sqlite3.Error:
                break
            if not events:
                break
            relevant = [e for e in events if lifecycle_kind(e[2], e[4], e[5], from_status=e[3]) is not None]
            try:
                decided = _decision_items_for(ledger, sorted({f"claim:{e[1]}" for e in relevant}))
            except LedgerReadError:  # do not advance past events that could not be joined
                break
            rows: list[OutcomeRecord] = []
            for event_id, claim_id, event_type, from_status, to_status, details, payload_json, created_at in relevant:
                matches = decided.get(f"claim:{claim_id}")
                if not matches:
                    continue
                try:
                    payload = json.loads(payload_json) if payload_json else None
                except ValueError:
                    payload = None
                kind = lifecycle_kind(event_type, to_status, details, from_status=from_status)
                source = label_source(event_type, details, payload)
                event_time = _parse_ts(created_at)
                for match in matches:
                    decided_at = _parse_ts(match["ts"])
                    if event_time is None or decided_at is None or event_time < decided_at.replace(microsecond=0):
                        continue
                    actor = payload.get("actor") if isinstance(payload, Mapping) else None
                    rows.append(OutcomeRecord(
                        match["decision_id"], match["item_ref"], str(kind), 1.0,
                        was_exposed=int(match["exposed"] or 0), outcome_window="lifecycle",
                        reward_version="lifecycle.v1", label_source=source, observed_at=str(created_at),
                        lag_s=_lag(event_time, decided_at),
                        details_json=_dumps({"event_id": event_id, "event_type": event_type,
                                             "to_status": to_status, "actor": actor}),
                    ))
                    dream_ref = _released_dream_ref(match) if kind == "steward_confirmed" else None
                    if dream_ref is not None:  # a held candidate, released, then confirmed
                        rows.append(OutcomeRecord(
                            match["decision_id"], dream_ref, "held_later_confirmed", 1.0, was_exposed=0,
                            outcome_window="lifecycle", reward_version="held.v1", label_source=source,
                            observed_at=str(created_at), lag_s=_lag(event_time, decided_at),
                            details_json=_dumps({"event_id": event_id, "claim_ref": match["item_ref"],
                                                 "actor": actor}),
                        ))
            if rows:
                written = ledger.record_outcomes_checked(rows)
                if written is None:  # do not advance past events whose outcomes were not stored
                    break
                inserted += written
            watermark = int(events[-1][0])
            if not ledger.set_watermark(LIFECYCLE_WATERMARK, str(watermark)):
                break
            if len(events) < batch:
                break
    finally:
        conn.close()
    return inserted


# ---------------------------------------------------------- skills / held ---

def record_skill_invocation(ledger: DecisionLedger, *, session_key: str, skill_ref: str,
                            observed_at: str | None = None, window: timedelta = SKILL_WINDOW) -> int:
    """``skill_invoked`` on SKILLS decisions that suggested it, else ``skill_invoked_unsuggested``."""
    observed_raw = observed_at or utc_iso()
    observed = _parse_ts(observed_raw)
    if not session_key or observed is None:
        return 0
    try:
        decisions = ledger.query(
            "SELECT d.decision_id, d.ts, MAX(CASE WHEN i.item_ref = ? AND i.exposed = 1 THEN 1 ELSE 0 END) AS hit "
            "FROM decisions d LEFT JOIN decision_items i USING (decision_id) "
            "WHERE d.session_key = ? AND d.surface = 'skills' AND d.ts >= ? AND d.ts <= ? "
            "GROUP BY d.decision_id ORDER BY d.ts DESC",
            [skill_ref, session_key, utc_iso(observed - window), utc_iso(observed)],
        )
    except LedgerReadError:
        return 0
    if not decisions:
        return 0
    hits = [d for d in decisions if d["hit"]]
    targets = hits or decisions[:1]
    kind = "skill_invoked" if hits else "skill_invoked_unsuggested"
    rows = [OutcomeRecord(d["decision_id"], skill_ref, kind, 1.0, was_exposed=int(bool(hits)),
                          outcome_window="session", reward_version="skills.v1", label_source="detector",
                          observed_at=observed_raw, lag_s=_lag(observed, _parse_ts(d["ts"]))) for d in targets]
    return ledger.record_outcomes(rows)


def record_held_release(ledger: DecisionLedger, *, item_ref: str, actor: str, observed_at: str | None = None,
                        confirmed: bool | None = None) -> int:
    """Join a release of a ``held`` INGEST candidate (and its later verdict) to the hold decisions."""
    observed_raw = observed_at or utc_iso()
    observed = _parse_ts(observed_raw)
    try:
        held = ledger.query(
            "SELECT DISTINCT d.decision_id, d.ts FROM decisions d JOIN decision_items i USING (decision_id) "
            "WHERE d.surface = 'ingest' AND d.action_taken = ? AND i.item_ref = ?",
            [json.dumps("hold"), item_ref],
        )
    except LedgerReadError:
        return 0
    source = actor if actor in _ACTORS else "unknown"
    kinds = ["held_released"]
    if confirmed is True:
        kinds.append("held_later_confirmed")
    elif confirmed is False:
        kinds.append("held_later_rejected")
    rows = [OutcomeRecord(d["decision_id"], item_ref, kind, 1.0, was_exposed=0, outcome_window="release",
                          reward_version="held.v1", label_source=source, observed_at=observed_raw,
                          lag_s=_lag(observed, _parse_ts(d["ts"])))
            for d in held for kind in kinds]
    return ledger.record_outcomes(rows) if rows else 0


def steward_cycle_outcomes(db_path: str | Path, *, config: Any = None,
                           now: datetime | None = None) -> dict[str, Any]:
    """Steward-cycle wiring: tail lifecycle events every cycle, prune retention daily.

    With no ledger yet (Jev never ran) nothing is read, written or created.  The
    prune runs when its watermark is missing or at least a day old.
    """
    from memorymaster.decisions.config import DecisionConfig
    from memorymaster.decisions.ledger import prune_job

    effective = config or DecisionConfig.from_env()
    ledger = DecisionLedger(effective.decisions_db)
    if not ledger.exists():
        return {"ledger": False, "lifecycle_outcomes": 0, "pruned": None}
    inserted = tail_lifecycle(ledger, db_path)
    moment = now or datetime.now(timezone.utc)
    last = _parse_ts(ledger.get_watermark(PRUNE_WATERMARK))
    pruned = None
    if last is None or moment - last >= PRUNE_INTERVAL:
        pruned = prune_job(effective, now=moment)
        ledger.set_watermark(PRUNE_WATERMARK, utc_iso(moment))
    return {"ledger": True, "lifecycle_outcomes": inserted, "pruned": pruned}


def record_outcome(ledger: DecisionLedger, decision_id: str, item_ref: str, kind: str, *, value: float = 1.0,
                   label_source: str = "detector", observed_at: str | None = None,
                   details: Mapping[str, Any] | None = None, was_exposed: int | None = None) -> int:
    """Generic append for other joiners (requery, correction, ...)."""
    if not ledger.exists():
        return 0
    return ledger.record_outcomes([OutcomeRecord(
        decision_id, item_ref, kind, value, was_exposed=was_exposed, label_source=label_source,
        observed_at=observed_at or utc_iso(), details_json=_dumps(details) if details else None,
    )])



__all__ = [
    "CLAIM_LINK_KIND",
    "LIFECYCLE_WATERMARK",
    "PRUNE_WATERMARK",
    "claim_lookup",
    "detect_usage",
    "distinctive_ngrams",
    "label_source",
    "lifecycle_kind",
    "record_held_release",
    "record_outcome",
    "record_skill_invocation",
    "record_turn_usage",
    "steward_cycle_outcomes",
    "tail_lifecycle",
]
