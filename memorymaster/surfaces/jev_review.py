"""Operator view of Jev decisions: metrics payload, weekly review queue, operator labels.

Shared by the dashboard ``Decisions`` tab and the ``jev-*`` CLI commands.  Reads go
through :class:`ReadOnlyLedger` (``mode=ro`` + ``query_only``), so viewing never
creates, migrates or writes the decisions ledger.  The only write is an operator
label: an append-only ``operator_review`` outcome recorded with
``decisions.outcomes.record_outcome(..., label_source="operator")``.

The review queue picks about 20 decisions for information, not recency: answers
near a stored threshold, Jev vs legacy disagreement, Jev vs steward disagreement
(a trusted lifecycle outcome contradicting the answer), paraphrase disagreement
(two wordings of one question disagree), then one per uncovered score decile.
Selection is deterministic (ties broken by a hash) and skips labelled items.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from operator import attrgetter
from typing import Any, Iterator, Mapping, NamedTuple, Sequence
from urllib.parse import quote

from memorymaster.decisions.ledger import LedgerReadError, utc_iso
from memorymaster.decisions.metrics import is_skip, stream_rows

REVIEW_KIND = "operator_review"
REVIEW_DAYS = 7
REVIEW_SIZE = 20
NEAR_THRESHOLD = 0.10
PARAPHRASE_PAIRS: tuple[tuple[str, str], ...] = (("memory.supersedes", "memory.supersedes_alt"),)
PARAPHRASE_GAP = 0.30
VERDICTS = ("correct", "incorrect")
REASONS = ("near_threshold", "legacy_disagreement", "steward_disagreement", "paraphrase_disagreement")
# Lifecycle outcomes that confirm or contradict a "yes" answer, from sources that may grade Jev.
POSITIVE_OUTCOMES = frozenset({"steward_confirmed", "proposal_approved", "held_later_confirmed"})
NEGATIVE_OUTCOMES = frozenset({"archived", "superseded", "conflicted", "proposal_rejected", "held_later_rejected"})
GRADING_SOURCES = frozenset({"steward", "operator"})
# Questions where a high answer means "keep / relevant / true" (a negative outcome contradicts it).
_POSITIVE_PREFIXES = ("lifecycle.", "ingest.", "recall.relevant", "recall.usable_evidence", "session.",
                      "memory.same_fact", "memory.contradicts", "memory.supersedes", "memory.same_scope")
# A pair question (dedup) is graded only by its proposal's verdict: a lifecycle event on the pair
# ('superseded' after memory.supersedes=yes, 'conflicted' after memory.contradicts=yes) agrees with Jev.
_PAIR_PREFIX = "memory."
_PROPOSAL_OUTCOMES = frozenset({"proposal_approved", "proposal_rejected"})
_MAX_FIELD = 200
_ID_CHUNK = 400


class ReadOnlyLedger:
    """``DecisionLedger``-compatible read view that can never write or create the file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.is_file()

    def _connect(self) -> sqlite3.Connection:
        from memorymaster.stores._storage_shared import connect_ro

        try:
            # Percent-encode: '%' and '#' are URI syntax ('#' would drop mode=ro and create a file).
            return connect_ro(quote(self.path.as_posix(), safe="/:"))
        except sqlite3.Error as exc:
            raise LedgerReadError("decision ledger could not be opened read-only") from exc

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Rows as dicts; ``[]`` for an absent ledger; ``LedgerReadError`` when unreadable."""
        if not self.exists():
            return []
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
        except sqlite3.Error as exc:
            raise LedgerReadError("decision ledger read failed") from exc
        finally:
            conn.close()

    def iter_rows(self, sql: str, params: Sequence[Any] = ()) -> Iterator[tuple[Any, ...]]:
        """Rows as plain tuples, one at a time (``query`` without a dict per row or a full list)."""
        if not self.exists():
            return
        conn = self._connect()
        try:
            conn.row_factory = None
            yield from conn.execute(sql, tuple(params))
        except sqlite3.Error as exc:
            raise LedgerReadError("decision ledger read failed") from exc
        finally:
            conn.close()


def default_ledger_path() -> Path:
    from memorymaster.decisions.config import DecisionConfig

    return Path(DecisionConfig.from_env().decisions_db)


def read_ledger(path: str | Path | None = None) -> ReadOnlyLedger:
    return ReadOnlyLedger(path if path is not None else default_ledger_path())


LEDGER_FILE_SUFFIXES = ("", "-wal", "-shm", "-journal")


def _same_file(a: Path, b: Path) -> bool:
    try:
        if a.exists() and b.exists():
            return os.path.samefile(a, b)  # also catches case differences, links and junctions
    except OSError:
        pass
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def refuse_ledger_output(ledger: ReadOnlyLedger, output: str | Path) -> Path:
    """Reject an export target that is the ledger or one of its SQLite side files.

    ``decisions.export.export_jsonl`` opens its target for writing before it reads:
    pointed at the ledger it would truncate the append-only decision record.
    """
    target = Path(output)
    for suffix in LEDGER_FILE_SUFFIXES:
        if _same_file(target, Path(f"{ledger.path}{suffix}")):
            raise ValueError(f"--output must not be the decisions ledger{f' {suffix} file' if suffix else ''}")
    return target


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _loads(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0.0 <= number <= 1.0 else None


# ------------------------------------------------------------------ metrics ---

def metrics_payload(ledger: Any, config: Any = None, *, now: datetime | None = None,
                    days: int = REVIEW_DAYS) -> dict[str, Any]:
    """``compute_metrics`` over the last ``days`` plus configured modes, cap and today's spend."""
    from memorymaster.decisions.config import SURFACES, DecisionConfig
    from memorymaster.decisions.metrics import compute_metrics

    effective = config or DecisionConfig.from_env()
    end = _now(now)
    report = compute_metrics(ledger, since=end - timedelta(days=max(1, int(days))), until=end,
                             daily_usd_cap=effective.daily_usd_cap)
    day_start = end.replace(hour=0, minute=0, second=0, microsecond=0)
    spent = ledger.query("SELECT COALESCE(SUM(cost_usd), 0.0) AS spend FROM decisions WHERE ts >= ?",
                         (utc_iso(day_start),))
    return {
        "ok": True,
        **report,
        "days": max(1, int(days)),
        "configured_modes": {surface: effective.mode_for(surface) for surface in SURFACES},
        "daily_usd_cap": effective.daily_usd_cap,
        "cost_today_usd": float(spent[0]["spend"]) if spent else 0.0,
        "ledger_present": bool(ledger.exists()),
    }


# -------------------------------------------------------------- review queue ---

def _question_text(question_id: str, version: Any) -> str | None:
    from memorymaster.decisions.questions import get

    try:
        return get(question_id, int(version)).instructions
    except (KeyError, TypeError, ValueError):
        return None


def _thresholds_for(decision: Mapping[str, Any], question_id: str, version: Any) -> dict[str, float]:
    stored = _loads(decision.get("thresholds_json"))
    if not isinstance(stored, dict):
        return {}
    values = stored.get(f"{question_id}@v{version}")
    if not isinstance(values, dict):
        return {}
    return {name: float(v) for name, v in values.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}


def _labelled(ledger: Any, decision_ids: Sequence[str]) -> set[tuple[str, str, str]]:
    done: set[tuple[str, str, str]] = set()
    for start in range(0, len(decision_ids), _ID_CHUNK):
        chunk = list(decision_ids[start:start + _ID_CHUNK])
        rows = ledger.query(
            f"SELECT decision_id, item_ref, details_json FROM outcomes WHERE kind = ? "
            f"AND decision_id IN ({', '.join('?' for _ in chunk)})", [REVIEW_KIND, *chunk])
        for row in rows:
            details = _loads(row["details_json"])
            question = details.get("question_id") if isinstance(details, dict) else None
            done.add((row["decision_id"], row["item_ref"], str(question or "")))
    return done


def _steward_disagrees(question_id: str, answer: float | None, kinds: set[str]) -> bool:
    if answer is None or not question_id.startswith(_POSITIVE_PREFIXES):
        return False
    if question_id.startswith(_PAIR_PREFIX):
        kinds = kinds & _PROPOSAL_OUTCOMES
    return (answer >= 0.5 and bool(kinds & NEGATIVE_OUTCOMES)) or (answer < 0.5 and bool(kinds & POSITIVE_OUTCOMES))


_QUEUE_DECISION_FIELDS = ("decision_id", "ts", "surface", "mode", "legacy_action", "jev_action", "action_taken",
                          "exploration_arm", "thresholds_json", "fallback_reason")
_QUEUE_ITEMS_SQL = (
    # The tie-break hash input ``decision_id|item_ref|question_id`` is built by SQLite as UTF-8 bytes.
    "SELECT i.decision_id, i.item_ref, i.question_id, i.question_version, i.answer, "
    "CAST(i.decision_id || '|' || i.item_ref || '|' || i.question_id AS BLOB) AS tiebreak_input "
    "FROM decision_items i JOIN decisions d USING (decision_id) WHERE d.ts >= ? AND d.ts <= ? "
    "AND i.question_id != '' AND i.item_ref IS NOT NULL")
_NO_KINDS: frozenset[str] = frozenset()


class _Candidate(NamedTuple):
    """One unlabelled answered item, kept compact: the queue considers every item of the window.

    Tuples order by uncertainty (distance to the nearest threshold, else to 0.5),
    then by a stable hash that is unique per item and question.
    """

    spread: float
    tiebreak: bytes
    decision_id: str
    item_ref: str
    question_id: str
    version: Any
    answer: float | None
    distance: float | None


def select_review_queue(ledger: Any, *, now: datetime | None = None, days: int = REVIEW_DAYS,
                        size: int = REVIEW_SIZE) -> list[dict[str, Any]]:
    """About ``size`` unlabelled decision items from the last ``days``, chosen for information.

    The window's item rows are streamed once into compact candidates; the redacted
    request (``state_redacted``), probabilities and item kinds are read only for the
    chosen items.  Items of a ``skip:`` decision (Jev was not asked) are never queued.
    """
    end = _now(now)
    size = max(1, int(size))
    window = (utc_iso(end - timedelta(days=max(1, int(days)))), utc_iso(end))
    decisions = {row[0]: dict(zip(_QUEUE_DECISION_FIELDS, row)) for row in stream_rows(
        ledger, f"SELECT {', '.join(_QUEUE_DECISION_FIELDS)} FROM decisions WHERE ts >= ? AND ts <= ?", window)}
    if not decisions:
        return []
    outcomes: dict[tuple[str, str], set[str]] = defaultdict(set)
    for decision_id, item_ref, kind, label_source in stream_rows(
            ledger, "SELECT o.decision_id, o.item_ref, o.kind, o.label_source FROM outcomes o JOIN decisions d "
            "USING (decision_id) WHERE d.ts >= ? AND d.ts <= ?", window):
        if label_source in GRADING_SOURCES:
            outcomes[(decision_id, item_ref)].add(kind)
    labelled = _labelled(ledger, list(decisions))

    partners = {second for _, second in PARAPHRASE_PAIRS}
    firsts = {first for first, _ in PARAPHRASE_PAIRS}
    answers: dict[tuple[str, str, str], float | None] = {}  # paraphrase partners' answers
    cuts: dict[tuple[str, str, Any], list[float]] = {}
    disagrees: dict[str, bool] = {}
    shared: dict[str, str] = {}  # one string object per item ref / question id
    near: list[_Candidate] = []
    steward: list[_Candidate] = []
    paraphrased: list[_Candidate] = []
    legacy_best: dict[str, _Candidate] = {}
    by_decile: list[list[_Candidate]] = [[] for _ in range(10)]
    for decision_id, item_ref, question_id, version, raw, tiebreak_input in stream_rows(
            ledger, _QUEUE_ITEMS_SQL, window):
        decision = decisions.get(decision_id)
        if decision is None:
            continue
        value = _number(raw)
        if question_id in partners:
            answers[(decision_id, item_ref, question_id)] = value
        if (decision_id, item_ref, question_id) in labelled or is_skip(decision["fallback_reason"]):
            continue
        decision_id = decision["decision_id"]
        item_ref = shared.setdefault(item_ref, item_ref)
        question_id = shared.setdefault(question_id, question_id)
        key = (decision_id, question_id, version)
        near_cuts = cuts.get(key)
        if near_cuts is None:
            near_cuts = cuts[key] = [v for v in _thresholds_for(decision, question_id, version).values()
                                     if 0.0 < v < 1.0]
        distance = None
        if value is not None:
            for cut in near_cuts:
                gap = abs(value - cut)
                if distance is None or gap < distance:
                    distance = gap
        spread = distance if distance is not None else (abs(value - 0.5) if value is not None else 1.0)
        c = _Candidate(spread, hashlib.sha256(tiebreak_input).digest(), decision_id, item_ref, question_id,
                       version, value, distance)
        if distance is not None and distance <= NEAR_THRESHOLD:
            near.append(c)
        if _steward_disagrees(question_id, value, outcomes.get((decision_id, item_ref), _NO_KINDS)):
            steward.append(c)
        if question_id in firsts:
            paraphrased.append(c)
        disagreeing = disagrees.get(decision_id)
        if disagreeing is None:
            disagreeing = disagrees[decision_id] = decision["jev_action"] is not None and _loads(
                decision["jev_action"]) != _loads(decision["legacy_action"])
        if disagreeing:  # a decision-level fact: review its most uncertain answer only
            best = legacy_best.get(decision_id)
            if best is None or c < best:
                legacy_best[decision_id] = c
        if value is not None:
            by_decile[min(int(value * 10), 9)].append(c)

    def paraphrase(c: _Candidate) -> tuple[str, float] | None:
        for first, second in PARAPHRASE_PAIRS:
            if c.question_id == first:
                other = answers.get((c.decision_id, c.item_ref, second))
                if c.answer is not None and other is not None and abs(c.answer - other) >= PARAPHRASE_GAP:
                    return second, other
        return None

    categories: dict[str, list[_Candidate]] = {
        "near_threshold": near, "legacy_disagreement": list(legacy_best.values()),
        "steward_disagreement": steward,
        "paraphrase_disagreement": [c for c in paraphrased if paraphrase(c) is not None],
    }
    for name in REASONS:
        categories[name].sort()

    chosen: list[tuple[_Candidate, str]] = []
    used_items: set[tuple[str, str]] = set()

    def take(c: _Candidate, reason: str) -> bool:
        key = (c.decision_id, c.item_ref)
        if key in used_items or len(chosen) >= size:
            return False
        used_items.add(key)
        chosen.append((c, reason))
        return True

    # 1) Targeted categories, round-robin, leaving room for the score deciles.
    targeted_room = max(size - 10, size // 2)
    cursors = {name: 0 for name in REASONS}

    def round_robin(limit: int) -> None:
        progress = True
        while progress and len(chosen) < limit:
            progress = False
            for name in REASONS:
                queue = categories[name]
                while cursors[name] < len(queue):
                    c = queue[cursors[name]]
                    cursors[name] += 1
                    if take(c, name):
                        progress = True
                        break
                if len(chosen) >= limit:
                    return

    round_robin(targeted_room)
    # 2) One representative per score decile not yet covered.
    covered = {min(int(c.answer * 10), 9) for c, _ in chosen if c.answer is not None}
    for decile in range(10):
        if decile in covered:
            continue
        for c in sorted(by_decile[decile], key=attrgetter("tiebreak")):
            if take(c, "score_decile"):
                break
    # 3) Remaining room: more of the targeted categories.
    round_robin(size)

    items, states = _chosen_rows(ledger, [c for c, _ in chosen])
    queue: list[dict[str, Any]] = []
    for c, picked_for in chosen:
        decision = decisions[c.decision_id]
        flags = {
            "near_threshold": c.distance is not None and c.distance <= NEAR_THRESHOLD,
            "legacy_disagreement": legacy_best.get(c.decision_id) is c,
            "steward_disagreement": _steward_disagrees(c.question_id, c.answer,
                                                       outcomes.get((c.decision_id, c.item_ref), set())),
            "paraphrase_disagreement": paraphrase(c) is not None,
        }
        reasons = [name for name in REASONS if flags[name]]
        if picked_for == "score_decile":
            reasons.append("score_decile")
        item_kind, raw_answer, probabilities = items.get((c.decision_id, c.item_ref, c.question_id), (None, None, None))
        state = _loads(states.get(c.decision_id))
        subjects = state.get("subjects") if isinstance(state, dict) else None
        other = paraphrase(c)
        queue.append({
            "decision_id": c.decision_id,
            "ts": decision["ts"],
            "surface": decision["surface"],
            "mode": decision["mode"],
            "item_ref": c.item_ref,
            "item_kind": item_kind,
            "question_id": c.question_id,
            "question_version": c.version,
            "question": _question_text(c.question_id, c.version),
            "answer": c.answer if c.answer is not None else raw_answer,
            "probabilities": _loads(probabilities),
            "thresholds": _thresholds_for(decision, c.question_id, c.version),
            "threshold_distance": c.distance,
            "reasons": reasons,
            "legacy_action": _loads(decision["legacy_action"]),
            "jev_action": _loads(decision["jev_action"]),
            "action_taken": _loads(decision["action_taken"]),
            "exploration_arm": decision["exploration_arm"],
            "lifecycle_outcomes": sorted(outcomes.get((c.decision_id, c.item_ref), set())),
            "paraphrase": {"question_id": other[0], "answer": other[1]} if other else None,
            "state": state.get("state") if isinstance(state, dict) else None,
            "subject": subjects.get(c.item_ref) if isinstance(subjects, dict) else None,
        })
    return queue


def _chosen_rows(ledger: Any, chosen: Sequence[_Candidate]
                 ) -> tuple[dict[tuple[str, str, str], tuple[Any, Any, Any]], dict[str, Any]]:
    """Item kind, raw answer and probabilities of the chosen items, and their decisions' redacted state."""
    wanted = {(c.decision_id, c.item_ref, c.question_id) for c in chosen}
    ids = sorted({c.decision_id for c in chosen})
    items: dict[tuple[str, str, str], tuple[Any, Any, Any]] = {}
    states: dict[str, Any] = {}
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start:start + _ID_CHUNK]
        marks = ", ".join("?" for _ in chunk)
        for decision_id, item_ref, question_id, item_kind, answer, probabilities in stream_rows(
                ledger, "SELECT decision_id, item_ref, question_id, item_kind, answer, probabilities_json "
                f"FROM decision_items WHERE decision_id IN ({marks})", chunk):
            if (decision_id, item_ref, question_id) in wanted:
                items[(decision_id, item_ref, question_id)] = (item_kind, answer, probabilities)
        for decision_id, state in stream_rows(
                ledger, f"SELECT decision_id, state_redacted FROM decisions WHERE decision_id IN ({marks})", chunk):
            states[decision_id] = state
    return items, states


# ------------------------------------------------------------------- labels ---

def _field(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > _MAX_FIELD or (not value and not allow_empty):
        raise ValueError(f"invalid {name}")
    return value


def record_review_label(ledger: Any, *, decision_id: Any, item_ref: Any, question_id: Any, verdict: Any,
                        now: datetime | None = None) -> dict[str, Any]:
    """Append the operator's verdict on one answered item; ``ValueError`` for unknown items."""
    from memorymaster.decisions.outcomes import record_outcome

    decision_id = _field(decision_id, "decision_id")
    item_ref = _field(item_ref, "item_ref", allow_empty=True)
    question_id = _field(question_id, "question_id")
    if verdict not in VERDICTS:
        raise ValueError("verdict must be 'correct' or 'incorrect'")
    if not ledger.exists():
        raise ValueError("no decisions ledger")
    rows = ledger.query(
        "SELECT question_version, answer, exposed FROM decision_items "
        "WHERE decision_id = ? AND item_ref = ? AND question_id = ?", (decision_id, item_ref, question_id))
    if not rows:
        raise ValueError("unknown decision item")
    answer = _number(rows[0]["answer"])
    correct = verdict == "correct"
    truth = None if answer is None else int((answer >= 0.5) == correct)
    details = {"question_id": question_id, "question_version": rows[0]["question_version"], "verdict": verdict,
               "answer": answer if answer is not None else rows[0]["answer"], "truth": truth}
    written = record_outcome(ledger, decision_id, item_ref, REVIEW_KIND, value=1.0 if correct else 0.0,
                             label_source="operator", observed_at=utc_iso(_now(now)), details=details,
                             was_exposed=int(rows[0]["exposed"] or 0))
    if written != 1:
        raise RuntimeError("operator label was not recorded")
    return {"ok": True, "decision_id": decision_id, "item_ref": item_ref, "question_id": question_id,
            "verdict": verdict, "truth": truth}


__all__ = [
    "REVIEW_KIND",
    "ReadOnlyLedger",
    "default_ledger_path",
    "metrics_payload",
    "read_ledger",
    "record_review_label",
    "refuse_ledger_output",
    "select_review_queue",
]
