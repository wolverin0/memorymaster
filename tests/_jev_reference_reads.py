"""Frozen copy of the Jev read paths before the 4.9.0 wave-3 performance rewrite (differential oracle).

``compute_metrics`` (``decisions/metrics.py``), ``select_review_queue``
(``surfaces/jev_review.py``) and ``_jev_ece_rises`` (``operations/operational_review.py``)
exactly as they were at 166ea91: ``SELECT *``, one dict per row, four
``compute_metrics`` calls per operational review.  The optimized implementations
must return identical results on any ledger without ``skip:`` rows (those rows
were introduced after this snapshot).  Do not edit the bodies below except to
follow a deliberate, documented change of semantics in the live code.
"""
# ruff: noqa
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from memorymaster.decisions.ledger import DecisionLedger, ledger_write_failures, utc_iso
from memorymaster.decisions.metrics import (
    DEFAULT_CALIBRATION,
    DEFAULT_MATURITY,
    HELD_ACTION,
    LIFECYCLE_MATURITY,
    UNTRUSTED_LABEL_SOURCES,
    _histogram,
    brier,
    ece,
    percentile,
    psi,
    reliability_bins,
)
from memorymaster.operations.operational_review import JEV_ECE_MIN_ITEMS, JEV_ECE_RISE
from memorymaster.surfaces.jev_review import (
    _PAIR_PREFIX,
    _POSITIVE_PREFIXES,
    _PROPOSAL_OUTCOMES,
    GRADING_SOURCES,
    NEAR_THRESHOLD,
    NEGATIVE_OUTCOMES,
    PARAPHRASE_GAP,
    PARAPHRASE_PAIRS,
    POSITIVE_OUTCOMES,
    REASONS,
    REVIEW_DAYS,
    REVIEW_KIND,
    REVIEW_SIZE,
    _now,
)


def _answer_value(answer: str | None) -> float | None:
    try:
        value = float(answer) if answer is not None else None
    except ValueError:
        return None
    return value if value is not None and math.isfinite(value) and 0.0 <= value <= 1.0 else None


def _arm(row: Mapping[str, Any]) -> str:
    if str(row.get("exploration_arm") or "").startswith("explore"):
        return "explored"
    if row.get("mode") == "live" and not row.get("fallback_reason"):
        return "jev"
    return "legacy"


def compute_metrics(
    ledger: DecisionLedger,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    daily_usd_cap: float | None = None,
    calibration: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    end = until or datetime.now(timezone.utc)
    start = since or end - timedelta(days=7)
    window = (utc_iso(start), utc_iso(end))
    decisions = ledger.query("SELECT * FROM decisions WHERE ts >= ? AND ts <= ? ORDER BY ts", window)
    items = ledger.query(
        "SELECT i.*, d.ts AS decision_ts FROM decision_items i JOIN decisions d USING (decision_id) "
        "WHERE d.ts >= ? AND d.ts <= ?", window,
    )
    outcomes = ledger.query(
        "SELECT o.decision_id, o.item_ref, o.kind, o.label_source FROM outcomes o JOIN decisions d "
        "USING (decision_id) WHERE d.ts >= ? AND d.ts <= ?", window,
    )
    outcome_set = {(o["decision_id"], o["item_ref"], o["kind"]) for o in outcomes}
    trusted_set = {(o["decision_id"], o["item_ref"], o["kind"]) for o in outcomes
                   if o["label_source"] not in UNTRUSTED_LABEL_SOURCES}
    by_decision = {row["decision_id"]: row for row in decisions}

    surfaces: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in decisions:
        grouped[row["surface"] or ""].append(row)
    for surface, rows in grouped.items():
        latencies = [r["latency_ms"] for r in rows if (r["attempt_count"] or 0) > 0 and r["latency_ms"] is not None]
        engine = [r["engine_ms"] for r in rows if r["engine_ms"] is not None]  # every decision, sent or not
        with_jev = [r for r in rows if r["jev_action"] is not None]
        breaker_opens = 0
        previous_open = False
        for r in rows:
            is_open = r["fallback_reason"] == "breaker_open"
            breaker_opens += int(is_open and not previous_open)
            previous_open = is_open
        surfaces[surface] = {
            "volume": len(rows),
            "live_pct": sum(1 for r in rows if r["mode"] == "live" and not r["fallback_reason"]) / len(rows),
            "modes": dict(Counter(r["mode"] for r in rows)),
            "fallback_by_reason": dict(Counter(r["fallback_reason"] for r in rows if r["fallback_reason"])),
            "latency_ms": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95),
                           "p99": percentile(latencies, 99), "n": len(latencies)},
            "engine_ms": {"p50": percentile(engine, 50), "p95": percentile(engine, 95),
                          "p99": percentile(engine, 99), "n": len(engine)},
            "tokens_in": sum(r["tokens_in"] or 0 for r in rows),
            "tokens_out": sum(r["tokens_out"] or 0 for r in rows),
            "cost_usd": sum(r["cost_usd"] or 0.0 for r in rows),
            "breaker_opens": breaker_opens,
            "egress_blocked": sum(1 for r in rows if r["fallback_reason"] == "egress_blocked"),
            "agreement_with_legacy": (sum(1 for r in with_jev if r["jev_action"] == r["legacy_action"])
                                      / len(with_jev)) if with_jev else None,
            "exposure_use": _exposure_use(surface, items, by_decision, outcome_set),
        }

    per_day: dict[str, float] = defaultdict(float)
    for row in decisions:
        per_day[str(row["ts"])[:10]] += row["cost_usd"] or 0.0
    cost_per_day = [{"day": day, "cost_usd": cost, "cap": daily_usd_cap,
                     "over_cap": (cost > daily_usd_cap) if daily_usd_cap is not None else None}
                    for day, cost in sorted(per_day.items())]

    return {
        "window": {"since": window[0], "until": window[1]},
        "surfaces": surfaces,
        "cost_per_day": cost_per_day,
        "questions": _distributions(items),
        "calibration": _calibration(items, trusted_set, end, calibration or DEFAULT_CALIBRATION, by_decision),
        "drift": _drift(ledger, end),
        "ledger_write_failures": ledger_write_failures(),
        # Added with send intents (not part of the streaming rewrite the differential pins).
        "orphan_send_intents": len(ledger.query(
            "SELECT i.decision_id FROM send_intents i LEFT JOIN decisions d USING (decision_id) "
            "WHERE i.ts >= ? AND i.ts <= ? AND d.decision_id IS NULL", window)),
    }


def _exposure_use(surface: str, items: Sequence[Mapping[str, Any]], by_decision: Mapping[str, Mapping[str, Any]],
                  outcome_set: set[tuple[str, str, str]]) -> dict[str, dict[str, Any]]:
    exposed: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for item in items:
        decision = by_decision.get(item["decision_id"])
        if decision is None or decision["surface"] != surface or not item["exposed"] or not item["item_ref"]:
            continue
        exposed[_arm(decision)].add((item["decision_id"], item["item_ref"]))
    result: dict[str, dict[str, Any]] = {}
    for arm, pairs in exposed.items():
        used = sum(1 for d, ref in pairs if (d, ref, "used_in_turn") in outcome_set)
        result[arm] = {"exposed": len(pairs), "used": used, "rate": used / len(pairs) if pairs else None}
    return result


def _distributions(items: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    values: dict[str, list[float]] = defaultdict(list)
    choices: dict[str, Counter[str]] = defaultdict(Counter)
    for item in items:
        if not item["question_id"]:
            continue
        key = f"{item['question_id']}@v{item['question_version']}"
        value = _answer_value(item["answer"])
        if value is None:
            choices[key][str(item["answer"])] += 1
        else:
            values[key].append(value)
    report: dict[str, dict[str, Any]] = {}
    for key in sorted(set(values) | set(choices)):
        data = values.get(key, [])
        report[key] = {"n": len(data) + sum(choices[key].values()), "histogram": _histogram(data),
                       "mean": sum(data) / len(data) if data else None, "choices": dict(choices[key])}
    return report


def _calibration(items: Sequence[Mapping[str, Any]], outcome_set: set[tuple[str, str, str]], end: datetime,
                 mapping: Mapping[str, str], by_decision: Mapping[str, Mapping[str, Any]] | None = None
                 ) -> dict[str, dict[str, Any]]:
    pairs: dict[str, list[tuple[float, int]]] = defaultdict(list)
    kinds: dict[str, str] = {}
    for item in items:
        kind = mapping.get(item["question_id"] or "")
        value = _answer_value(item["answer"])
        if kind is None or value is None or not item["item_ref"]:
            continue
        if kind == "used_in_turn" and not item["exposed"]:
            continue  # use can only be observed for exposed items
        if kind not in DEFAULT_MATURITY and _is_held((by_decision or {}).get(item["decision_id"])):
            continue
        maturity = DEFAULT_MATURITY.get(kind, LIFECYCLE_MATURITY)
        decided = _parse(item["decision_ts"])
        if decided is None or decided > end - maturity:
            continue
        key = f"{item['question_id']}@v{item['question_version']}"
        kinds[key] = kind
        pairs[key].append((value, int((item["decision_id"], item["item_ref"], kind) in outcome_set)))
    report: dict[str, dict[str, Any]] = {}
    for key, data in pairs.items():
        probs, labels = [p for p, _ in data], [y for _, y in data]
        report[key] = {"n": len(data), "positive_kind": kinds[key], "base_rate": sum(labels) / len(labels),
                       "ece": ece(probs, labels), "brier": brier(probs, labels),
                       "bins": reliability_bins(probs, labels)}
    return report


def _is_held(decision: Mapping[str, Any] | None) -> bool:
    if not decision or decision.get("action_taken") is None:
        return False
    try:
        return json.loads(decision["action_taken"]) == HELD_ACTION
    except (TypeError, ValueError):
        return False


def _drift(ledger: DecisionLedger, end: datetime) -> dict[str, float | None]:
    current_start, previous_start = end - timedelta(days=7), end - timedelta(days=14)
    rows = ledger.query(
        "SELECT i.question_id, i.question_version, i.answer, d.ts FROM decision_items i "
        "JOIN decisions d USING (decision_id) WHERE d.ts >= ? AND d.ts <= ? AND i.question_id != ''",
        (utc_iso(previous_start), utc_iso(end)),
    )
    current: dict[str, list[float]] = defaultdict(list)
    previous: dict[str, list[float]] = defaultdict(list)
    boundary = utc_iso(current_start)
    for row in rows:
        value = _answer_value(row["answer"])
        if value is None:
            continue
        key = f"{row['question_id']}@v{row['question_version']}"
        (current if row["ts"] >= boundary else previous)[key].append(value)
    return {key: psi(previous.get(key, []), current[key]) for key in sorted(current)}


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---- jev_review ----

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


def _distance(answer: float | None, thresholds: Mapping[str, float]) -> float | None:
    cuts = [value for value in thresholds.values() if 0.0 < value < 1.0]
    if answer is None or not cuts:
        return None
    return min(abs(answer - cut) for cut in cuts)


def _tiebreak(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def _labelled(ledger: Any, decision_ids: Sequence[str]) -> set[tuple[str, str, str]]:
    done: set[tuple[str, str, str]] = set()
    for start in range(0, len(decision_ids), 400):
        chunk = list(decision_ids[start:start + 400])
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


def select_review_queue(ledger: Any, *, now: datetime | None = None, days: int = REVIEW_DAYS,
                        size: int = REVIEW_SIZE) -> list[dict[str, Any]]:
    """About ``size`` unlabelled decision items from the last ``days``, chosen for information."""
    end = _now(now)
    size = max(1, int(size))
    window = (utc_iso(end - timedelta(days=max(1, int(days)))), utc_iso(end))
    decisions = {row["decision_id"]: row for row in ledger.query(
        "SELECT decision_id, ts, surface, mode, legacy_action, jev_action, action_taken, exploration_arm, "
        "thresholds_json, state_redacted FROM decisions WHERE ts >= ? AND ts <= ?", window)}
    if not decisions:
        return []
    rows = [row for row in ledger.query(
        "SELECT i.decision_id, i.item_ref, i.item_kind, i.question_id, i.question_version, i.answer, "
        "i.probabilities_json FROM decision_items i JOIN decisions d USING (decision_id) "
        "WHERE d.ts >= ? AND d.ts <= ? AND i.question_id != '' AND i.item_ref IS NOT NULL", window)
        if row["decision_id"] in decisions]
    outcomes: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in ledger.query(
            "SELECT o.decision_id, o.item_ref, o.kind, o.label_source FROM outcomes o JOIN decisions d "
            "USING (decision_id) WHERE d.ts >= ? AND d.ts <= ?", window):
        if row["label_source"] in GRADING_SOURCES:
            outcomes[(row["decision_id"], row["item_ref"])].add(row["kind"])
    labelled = _labelled(ledger, list(decisions))

    answers: dict[tuple[str, str, str], float | None] = {}
    candidates: list[dict[str, Any]] = []
    for row in rows:
        value = _number(row["answer"])
        answers[(row["decision_id"], row["item_ref"], row["question_id"])] = value
        if (row["decision_id"], row["item_ref"], row["question_id"]) in labelled:
            continue
        decision = decisions[row["decision_id"]]
        thresholds = _thresholds_for(decision, row["question_id"], row["question_version"])
        candidates.append({"row": row, "decision": decision, "answer": value, "thresholds": thresholds,
                           "distance": _distance(value, thresholds),
                           "tiebreak": _tiebreak(row["decision_id"], row["item_ref"], row["question_id"])})

    def uncertainty(c: dict[str, Any]) -> tuple[float, str]:
        if c["distance"] is not None:
            return (c["distance"], c["tiebreak"])
        return (abs(c["answer"] - 0.5) if c["answer"] is not None else 1.0, c["tiebreak"])

    def paraphrase(c: dict[str, Any]) -> tuple[str, float] | None:
        row = c["row"]
        for first, second in PARAPHRASE_PAIRS:
            if row["question_id"] == first:
                other = answers.get((row["decision_id"], row["item_ref"], second))
                if c["answer"] is not None and other is not None and abs(c["answer"] - other) >= PARAPHRASE_GAP:
                    return second, other
        return None

    def disagrees_with_legacy(c: dict[str, Any]) -> bool:
        decision = c["decision"]
        return decision["jev_action"] is not None and _loads(decision["jev_action"]) != _loads(
            decision["legacy_action"])

    categories: dict[str, list[dict[str, Any]]] = {name: [] for name in REASONS}
    legacy_best: dict[str, dict[str, Any]] = {}
    for c in candidates:
        row = c["row"]
        c["flags"] = set()
        if c["distance"] is not None and c["distance"] <= NEAR_THRESHOLD:
            c["flags"].add("near_threshold")
        if _steward_disagrees(row["question_id"], c["answer"], outcomes.get((row["decision_id"], row["item_ref"]),
                                                                          set())):
            c["flags"].add("steward_disagreement")
        if paraphrase(c) is not None:
            c["flags"].add("paraphrase_disagreement")
        if disagrees_with_legacy(c):  # a decision-level fact: review its most uncertain answer only
            best = legacy_best.get(row["decision_id"])
            if best is None or uncertainty(c) < uncertainty(best):
                legacy_best[row["decision_id"]] = c
        for name in c["flags"]:
            categories[name].append(c)
    for c in legacy_best.values():
        c["flags"].add("legacy_disagreement")
        categories["legacy_disagreement"].append(c)
    for name in REASONS:
        categories[name].sort(key=uncertainty)

    chosen: list[dict[str, Any]] = []
    used_items: set[tuple[str, str]] = set()

    def take(c: dict[str, Any], reason: str) -> bool:
        key = (c["row"]["decision_id"], c["row"]["item_ref"])
        if key in used_items or len(chosen) >= size:
            return False
        used_items.add(key)
        chosen.append({**c, "picked_for": reason})
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
    covered = {min(int(c["answer"] * 10), 9) for c in chosen if c["answer"] is not None}
    by_decile: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for c in candidates:
        if c["answer"] is not None:
            by_decile[min(int(c["answer"] * 10), 9)].append(c)
    for decile in range(10):
        if decile in covered:
            continue
        for c in sorted(by_decile.get(decile, []), key=lambda c: c["tiebreak"]):
            if take(c, "score_decile"):
                break
    # 3) Remaining room: more of the targeted categories.
    round_robin(size)

    queue: list[dict[str, Any]] = []
    for c in chosen:
        row, decision = c["row"], c["decision"]
        reasons = [name for name in REASONS if name in c["flags"]]
        if c["picked_for"] == "score_decile":
            reasons.append("score_decile")
        state = _loads(decision["state_redacted"])
        subjects = state.get("subjects") if isinstance(state, dict) else None
        other = paraphrase(c)
        queue.append({
            "decision_id": row["decision_id"],
            "ts": decision["ts"],
            "surface": decision["surface"],
            "mode": decision["mode"],
            "item_ref": row["item_ref"],
            "item_kind": row["item_kind"],
            "question_id": row["question_id"],
            "question_version": row["question_version"],
            "question": _question_text(row["question_id"], row["question_version"]),
            "answer": c["answer"] if c["answer"] is not None else row["answer"],
            "probabilities": _loads(row["probabilities_json"]),
            "thresholds": c["thresholds"],
            "threshold_distance": c["distance"],
            "reasons": reasons,
            "legacy_action": _loads(decision["legacy_action"]),
            "jev_action": _loads(decision["jev_action"]),
            "action_taken": _loads(decision["action_taken"]),
            "exploration_arm": decision["exploration_arm"],
            "lifecycle_outcomes": sorted(outcomes.get((row["decision_id"], row["item_ref"]), set())),
            "paraphrase": {"question_id": other[0], "answer": other[1]} if other else None,
            "state": state.get("state") if isinstance(state, dict) else None,
            "subject": subjects.get(row["item_ref"]) if isinstance(subjects, dict) else None,
        })
    return queue


# ---- operational_review ----

def _jev_ece_rises(ledger, end: datetime) -> list[str]:
    """Compare the two most recent matured weeks per outcome maturity.

    ``compute_metrics`` only counts items decided at least one maturity before its
    ``until``: a week ending now holds no matured item for a 7-day lifecycle outcome
    (``ingest.usefulness`` -> ``steward_confirmed``), so each maturity class is
    compared on the weeks ending ``maturity`` before now.
    """
    week = timedelta(days=7)
    current: dict = {}
    previous: dict = {}
    for maturity in sorted({*DEFAULT_MATURITY.values(), LIFECYCLE_MATURITY}):
        def matures(entry: dict, maturity: timedelta = maturity) -> bool:
            return DEFAULT_MATURITY.get(entry.get("positive_kind"), LIFECYCLE_MATURITY) == maturity

        now_week = compute_metrics(ledger, since=end - maturity - week, until=end)["calibration"]
        week_before = compute_metrics(ledger, since=end - maturity - 2 * week, until=end - week)["calibration"]
        current.update({key: entry for key, entry in now_week.items() if matures(entry)})
        previous.update({key: entry for key, entry in week_before.items() if matures(entry)})
    rises = []
    for key in sorted(set(current) & set(previous)):
        now_c, before = current[key], previous[key]
        if min(now_c["n"], before["n"]) < JEV_ECE_MIN_ITEMS or now_c["ece"] is None or before["ece"] is None:
            continue
        if now_c["ece"] - before["ece"] > JEV_ECE_RISE:
            rises.append(f"{key} ece {before['ece']:.3f}->{now_c['ece']:.3f}")
    return rises
