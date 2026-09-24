"""Surface S3 INGEST: Jev triage of Dreaming candidates, and releasing held ones.

After extraction and before consolidation the worker asks Jev, once per
candidate, the eight source-review checks (``dreaming.source_review.CHECKS``)
over the candidate, its evidence quote and a bounded capture excerpt; the
decisions engine redacts everything before it leaves the machine.

* Live, every check at or above its threshold: ``admit`` (consolidation as usual).
* Otherwise ``hold``: the candidate stays in ``extraction_json`` with a
  ``jev_ingest`` annotation, consolidation skips it and the capture still
  completes. Ruling R7: retention exempts the capture for at most
  ``MEMORYMASTER_DREAM_HELD_RETAIN_DAYS`` (default 30) after the hold, never
  when it is quarantined; a held candidate pruned with its capture gets a
  ``held_expired`` outcome (``label_source`` ``surface``). 5 % of holds are
  admitted anyway (arm ``explore_admit``, propensity in the decisions ledger).
* Off, shadow, fallback (timeout, HTTP error, budget, breaker, missing key,
  blocked egress ...): the legacy path, i.e. no triage. Shadow and fallback
  decisions are still annotated ``admitted`` so a candidate is decided once.

Jev never deletes or rejects a candidate: :func:`release` un-holds candidates
(the next run consolidates them through the normal source review) and records
``held_released`` outcomes against the hold decisions.

Rewards: every triaged candidate that reaches consolidation (admitted,
explored or released) gets the consolidator's verdict on its decision
(``consolidation_applied`` / ``consolidation_rejected``, :func:`record_verdicts`),
and a claim its application creates is linked back to that decision
(:func:`record_claim_link`) so the steward's later lifecycle labels join it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

ANNOTATION = "jev_ingest"
ADMIT, HOLD = "admit", "hold"
ADMITTED, HELD, RELEASED = "admitted", "held", "released"
EXPLORE_ARM = "explore_admit"
ITEM_KIND = "dream_candidate"
EXCERPT_CHARS = 1_200
_ACTOR_CHARS = 40
#: Ruling R7: days a held capture is exempt from retention after its latest hold.
HELD_RETAIN_DAYS = 30
HELD_RETAIN_DAYS_ENV = "MEMORYMASTER_DREAM_HELD_RETAIN_DAYS"
HELD_EXPIRED = "held_expired"
CONSOLIDATION_APPLIED = "consolidation_applied"
CONSOLIDATION_REJECTED = "consolidation_rejected"


def item_ref(capture_id: int, candidate_id: str) -> str:
    """Decision-ledger item for one candidate of one capture."""
    return f"dream:{int(capture_id)}:{candidate_id}"


def annotation(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    note = payload.get(ANNOTATION)
    return dict(note) if isinstance(note, Mapping) else None


def status(payload: Mapping[str, Any]) -> str | None:
    note = annotation(payload)
    return str(note.get("status")) if note else None


def is_held(payload: Mapping[str, Any]) -> bool:
    return status(payload) == HELD


def held_count(candidates: Iterable[Mapping[str, Any]]) -> int:
    return sum(1 for payload in candidates if is_held(payload))


def released_undecided(candidates: Iterable[Mapping[str, Any]] | None,
                       decisions: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Released candidates without a consolidation decision yet: their capture is not done."""
    decided = {str(decision.get("candidate_id")) for decision in decisions or []}
    return [dict(payload) for payload in candidates or []
            if status(payload) == RELEASED and str(payload.get("candidate_id")) not in decided]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def held_since(candidates: Iterable[Mapping[str, Any]], *, fallback: Any = None) -> datetime | None:
    """When the capture's latest hold was decided (``decided_at``), else ``fallback``."""
    moments = [_parse_time((annotation(payload) or {}).get("decided_at"))
               for payload in candidates if is_held(payload)]
    known = [moment for moment in moments if moment is not None]
    return max(known) if known else _parse_time(fallback)


def expired_notes(capture_id: int, candidates: Iterable[Mapping[str, Any]], *, reason: str,
                  fallback: Any = None) -> list[dict[str, Any]]:
    """One entry per held candidate of a capture that retention is pruning."""
    payloads = list(candidates)
    since = held_since(payloads, fallback=fallback)
    notes = []
    for payload in payloads:
        note = annotation(payload)
        if note is None or note.get("status") != HELD:
            continue
        notes.append({
            "capture_id": int(capture_id),
            "candidate_id": str(payload.get("candidate_id")),
            "decision_id": note.get("decision_id"),
            "item_ref": str(note.get("item_ref") or item_ref(capture_id, str(payload.get("candidate_id")))),
            "held_since": since.isoformat() if since else None,
            "reason": reason,
        })
    return notes


def record_expired(decisions: Any, notes: Iterable[Mapping[str, Any]], *, observed_at: str | None = None) -> int:
    """Append ``held_expired`` outcomes (``label_source`` ``surface``) to the hold decisions.

    ``decisions`` is a ``DecisionLedger``, a path or ``None`` (the configured
    ``MEMORYMASTER_DECISIONS_DB``); an absent decisions ledger is not created.
    """
    from memorymaster.decisions.ledger import OutcomeRecord

    ledger = _decisions_ledger(decisions)
    if not ledger.exists():
        return 0
    moment = observed_at or _utc_now()
    rows = [OutcomeRecord(
        str(note["decision_id"]), str(note["item_ref"]), HELD_EXPIRED, 1.0, was_exposed=0,
        outcome_window="retention", reward_version="held.v1", label_source="surface", observed_at=moment,
        details_json=json.dumps({"capture_id": note.get("capture_id"), "held_since": note.get("held_since"),
                                 "reason": note.get("reason")}, sort_keys=True),
    ) for note in notes if note.get("decision_id")]
    return ledger.record_outcomes(rows) if rows else 0


def triage_note(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """The S3 annotation of a candidate that may reach consolidation (admitted or released)."""
    note = annotation(payload)
    if note and note.get("decision_id") and note.get("status") in (ADMITTED, RELEASED):
        return note
    return None


def triage_notes(candidates: Iterable[Mapping[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """``candidate_id`` -> :func:`triage_note` for a capture's extraction payloads."""
    notes = {}
    for payload in candidates or []:
        note = triage_note(payload)
        if note is not None:
            notes[str(payload.get("candidate_id"))] = note
    return notes


def record_verdicts(decisions: Any, capture_id: int, verdicts: Iterable[tuple[Mapping[str, Any], Any]], *,
                    observed_at: str | None = None) -> int:
    """The consolidator's verdict on each triaged candidate, on its S3 decision.

    ``verdicts`` pairs a :func:`triage_note` with the reviewed consolidation
    action: ``ignore`` is ``consolidation_rejected``, anything else
    ``consolidation_applied`` (``label_source`` ``steward``). An absent decisions
    ledger is not created.
    """
    from memorymaster.decisions.ledger import OutcomeRecord

    moment = observed_at or _utc_now()
    rows = []
    for note, action in verdicts:
        verb = str(action or "")
        rows.append(OutcomeRecord(
            str(note["decision_id"]), str(note["item_ref"]),
            CONSOLIDATION_REJECTED if verb == "ignore" else CONSOLIDATION_APPLIED, 1.0, was_exposed=0,
            outcome_window="consolidation", reward_version="ingest.v1", label_source="steward", observed_at=moment,
            details_json=json.dumps({"action": verb, "capture_id": int(capture_id), "status": note.get("status"),
                                     "arm": note.get("arm")}, sort_keys=True),
        ))
    if not rows:
        return 0
    ledger = _decisions_ledger(decisions)
    return ledger.record_outcomes(rows) if ledger.exists() else 0


def record_claim_link(decisions: Any, note: Mapping[str, Any], claim_id: int, *,
                      observed_at: str | None = None) -> int:
    """Link a claim created by an application to its candidate's S3 decision.

    ``decisions.outcomes.tail_lifecycle`` joins the claim's later lifecycle
    events (steward confirmed, archived, superseded ...) back to that decision
    through this row, and records ``held_later_confirmed`` for a released one.
    """
    from memorymaster.decisions.ledger import OutcomeRecord
    from memorymaster.decisions.outcomes import CLAIM_LINK_KIND

    ledger = _decisions_ledger(decisions)
    if not note.get("decision_id") or not ledger.exists():
        return 0
    return ledger.record_outcomes([OutcomeRecord(
        str(note["decision_id"]), f"claim:{int(claim_id)}", CLAIM_LINK_KIND, 1.0, was_exposed=0,
        outcome_window="application", reward_version="ingest.v1", label_source="surface",
        observed_at=observed_at or _utc_now(),
        details_json=json.dumps({"dream_ref": note.get("item_ref"), "released": note.get("status") == RELEASED},
                                sort_keys=True),
    )])


def candidate_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The ``DreamCandidate`` fields of an extraction payload, without the annotation."""
    return {key: value for key, value in payload.items() if key != ANNOTATION}


def annotate(candidates: Iterable[Mapping[str, Any]], notes: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Attach first decisions only: a candidate that already has one keeps it."""
    out: list[dict[str, Any]] = []
    for payload in candidates:
        note = notes.get(str(payload.get("candidate_id")))
        out.append({**payload, ANNOTATION: dict(note)} if note is not None and annotation(payload) is None
                   else dict(payload))
    return out


def _field(candidate: Any, name: str) -> str:
    value = candidate.get(name) if isinstance(candidate, Mapping) else getattr(candidate, name, "")
    return str(value or "")


def capture_excerpt(messages: Iterable[Mapping[str, Any]], candidate: Any, *, limit: int = EXCERPT_CHARS) -> str:
    """At most ``limit`` characters of the capture around the evidence quote.

    The window starts a third of the way before the quote so that later
    corrections are included; cut words at either end are dropped.
    """
    rows = list(messages)
    lines = [f"{row.get('role', '')}: {row.get('text', '')}" for row in rows]
    transcript = "\n".join(lines)
    if len(transcript) <= limit:
        return transcript
    anchor, offset = 0, 0
    for row, line in zip(rows, lines):
        if str(row.get("message_id") or row.get("id")) == _field(candidate, "evidence_message_id"):
            found = line.find(_field(candidate, "evidence_quote"))
            anchor = offset + max(found, 0)
            break
        offset += len(line) + 1
    start = max(0, min(anchor - limit // 3, len(transcript) - limit))
    window = transcript[start:start + limit]
    if start > 0 and not transcript[start - 1].isspace():
        window = window.split(None, 1)[-1] if len(window.split(None, 1)) > 1 else ""
    if start + limit < len(transcript) and not transcript[start + limit].isspace():
        window = window.rsplit(None, 1)[0] if len(window.rsplit(None, 1)) > 1 else ""
    return window.strip()


def ingest_choice(answers: Any, ref: str) -> Any:
    """Policy: admit only when all eight checks reach their ``admit`` threshold."""
    from memorymaster.decisions.engine import JevChoice
    from memorymaster.decisions.questions import INGEST_CHECKS

    passed = []
    for check in INGEST_CHECKS:
        question = f"ingest.{check}"
        value = answers.noul(question, ref)
        if value is None:  # an unanswered check is a failed request, not a verdict
            raise ValueError(f"missing answer for {question}")
        passed.append(value >= float(answers.threshold(question, "admit", 0.5)))
    if all(passed):
        return JevChoice(action=ADMIT)
    return JevChoice(action=HOLD, safe_alternative=ADMIT, explore_arm=EXPLORE_ARM)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def release(ledger_or_path: Any, *, capture_id: int | None = None, candidate_id: str | None = None,
            actor: str = "operator", decisions: Any = None) -> int:
    """Un-hold candidates and return how many were released.

    ``ledger_or_path`` is the Dreaming ledger (``DreamLedger`` or its path; a
    missing file releases nothing and is not created). With neither
    ``capture_id`` nor ``candidate_id`` every held candidate is released. A
    released candidate's capture is reopened (``consolidated``/``applied`` ->
    ``extracted``) so the next run consolidates it without asking Jev again.
    It is safe while a Dreaming run works on the capture: the ledger never
    marks a capture ``consolidated``/``applied`` while it has released,
    undecided candidates, so the run leaves it ``extracted`` instead.
    A ``quarantined`` capture is skipped (not counted, nothing recorded): no
    run consolidates it again, so its candidates stay held and retained.
    Each release appends ``held_released`` to the hold decisions in the
    decisions ledger (``decisions``: ``DecisionLedger``, path, or the configured
    ``MEMORYMASTER_DECISIONS_DB``); an absent decisions ledger is not created.
    """
    from memorymaster.dreaming.ledger import DreamLedger

    if isinstance(ledger_or_path, DreamLedger):
        ledger = ledger_or_path
    else:
        path = Path(ledger_or_path)
        if not path.is_file():
            return 0
        ledger = DreamLedger(path)
    who = str(actor or "operator")[:_ACTOR_CHARS]
    released_at = _utc_now()
    released: list[str] = []
    for held_capture in ledger.held_capture_ids(capture_id):
        hits: list[str] = []

        def change(candidates: list[dict[str, Any]], hits: list[str] = hits, owner: int = held_capture):
            out: list[dict[str, Any]] = []
            for payload in candidates:
                note = annotation(payload)
                if note and note.get("status") == HELD and candidate_id in (None, payload.get("candidate_id")):
                    note.update({"status": RELEASED, "released_at": released_at, "released_by": who})
                    payload = {**payload, ANNOTATION: note}
                    hits.append(str(note.get("item_ref") or item_ref(owner, str(payload.get("candidate_id")))))
                out.append(payload)
            return (out, held_count(out), True) if hits else None

        if ledger.update_extraction(held_capture, change, keep_states=("quarantined",)) is not None:
            released.extend(hits)
    if released:
        _record_releases(decisions, released, who, released_at)
    return len(released)


def _decisions_ledger(decisions: Any) -> Any:
    """A ``DecisionLedger`` for ``decisions`` (ledger, path, or ``None`` for the configured one)."""
    from memorymaster.decisions.ledger import DecisionLedger

    if isinstance(decisions, DecisionLedger):
        return decisions
    if decisions is None:
        from memorymaster.decisions.config import DecisionConfig

        decisions = DecisionConfig.from_env().decisions_db
    return DecisionLedger(decisions)


def _record_releases(decisions: Any, refs: list[str], actor: str, observed_at: str) -> None:
    from memorymaster.decisions.outcomes import record_held_release

    ledger = _decisions_ledger(decisions)
    for ref in refs:
        record_held_release(ledger, item_ref=ref, actor=actor, observed_at=observed_at)


__all__ = [
    "ADMIT",
    "ADMITTED",
    "ANNOTATION",
    "HELD",
    "HELD_EXPIRED",
    "HELD_RETAIN_DAYS",
    "HELD_RETAIN_DAYS_ENV",
    "HOLD",
    "CONSOLIDATION_APPLIED",
    "CONSOLIDATION_REJECTED",
    "RELEASED",
    "annotate",
    "annotation",
    "candidate_fields",
    "capture_excerpt",
    "expired_notes",
    "held_count",
    "held_since",
    "ingest_choice",
    "is_held",
    "item_ref",
    "record_claim_link",
    "record_expired",
    "record_verdicts",
    "release",
    "released_undecided",
    "status",
    "triage_note",
    "triage_notes",
]
