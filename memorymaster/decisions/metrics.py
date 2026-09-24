"""Aggregations over the decisions ledger for the dashboard and the operational review.

Health: volume, live %, fallback by reason, transport latency and whole-engine
time (``engine_ms``) p50/p95/p99, tokens, cost per
day vs cap, breaker opens, egress blocked.  Behaviour: answer distributions per
question version, agreement with the legacy action.  Outcomes: exposure->use
rates for Jev-driven, legacy and explored actions.  Calibration: reliability bins,
ECE and Brier per question version where outcomes exist (never from ``jev`` or
``unattributed_override`` labels, never for a held INGEST candidate).  Drift: weekly PSI of
answer distributions.  ``skip:`` rows (the surface did not ask Jev) count in volume
and fallbacks but never in latency, engine time, breaker opens or calibration.
Read-only; every number is computed from ledger rows.  An existing ledger that
cannot be read raises ``LedgerReadError`` instead of reporting zeros; an absent
ledger reports empty metrics.

Reads select only the columns they use (never ``state_redacted``), stream item rows
as tuples (``stream_rows``) and keep per-question aggregates rather than rows, so a
busy recall ledger (4000 decisions x 80 item rows) stays under 2 s and 150 MB
(``tests/test_jev_read_paths.py``, which also pins the results to the pre-optimization
code).
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Mapping, Sequence

from memorymaster.decisions.ledger import DecisionLedger, ledger_write_failures, utc_iso

BINS = 10
PSI_EPSILON = 1e-4
DEFAULT_CALIBRATION: Mapping[str, str] = {
    "recall.usable_evidence": "used_in_turn",
    "recall.relevant": "used_in_turn",
    "session.relevant_to_project": "used_in_turn",
    "lifecycle.useful_future": "used_in_turn",
    "ingest.usefulness": "steward_confirmed",
}
DEFAULT_MATURITY: Mapping[str, timedelta] = {"used_in_turn": timedelta(hours=1)}
LIFECYCLE_MATURITY = timedelta(days=7)
# Calibration labels: never Jev grading itself, never an override nobody attributed.
# Single source for every calibration/OPE/metrics consumer: labels whose actor
# is Jev itself or cannot be attributed never count as ground truth.
UNTRUSTED_LABEL_SOURCES = frozenset({"jev", "unattributed_override", "unknown_actor", "unknown"})
HELD_ACTION = "hold"  # a held INGEST candidate never reaches the steward: no verdict is not a "no"
# ``record_skip`` rows: the surface ran but did not ask Jev (``fallback_reason`` ``skip:<why>``,
# ``attempt_count`` 0).  Counted in volume and fallback_by_reason; never in latency, calibration
# or off-policy evaluation.
SKIP_PREFIX = "skip:"


def is_skip(fallback_reason: Any) -> bool:
    return isinstance(fallback_reason, str) and fallback_reason.startswith(SKIP_PREFIX)


# ------------------------------------------------------------ pure helpers ---

def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile."""
    data = sorted(v for v in values if v is not None)
    if not data:
        return None
    rank = max(1, math.ceil(q / 100 * len(data)))
    return data[min(rank, len(data)) - 1]


def brier(probs: Sequence[float], labels: Sequence[int]) -> float | None:
    if not probs:
        return None
    return sum((p - y) ** 2 for p, y in zip(probs, labels)) / len(probs)


def reliability_bins(probs: Sequence[float], labels: Sequence[int], bins: int = BINS) -> list[dict[str, Any]]:
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for p, y in zip(probs, labels):
        index = min(int(p * bins), bins - 1)
        buckets[index].append((p, y))
    result = []
    for index, bucket in enumerate(buckets):
        n = len(bucket)
        result.append({
            "lower": index / bins, "upper": (index + 1) / bins, "n": n,
            "confidence": sum(p for p, _ in bucket) / n if n else None,
            "accuracy": sum(y for _, y in bucket) / n if n else None,
        })
    return result


def ece(probs: Sequence[float], labels: Sequence[int], bins: int = BINS) -> float | None:
    if not probs:
        return None
    total = len(probs)
    return sum(b["n"] / total * abs(b["accuracy"] - b["confidence"])
               for b in reliability_bins(probs, labels, bins) if b["n"])


def _histogram(values: Iterable[float], bins: int = BINS) -> list[int]:
    counts = [0] * bins
    for v in values:
        counts[min(int(v * bins), bins - 1)] += 1
    return counts


def psi(expected: Sequence[float], actual: Sequence[float], bins: int = BINS) -> float | None:
    """Population stability index of ``actual`` against ``expected`` over [0, 1] bins."""
    if not expected or not actual:
        return None
    return _psi_counts(_histogram(expected, bins), _histogram(actual, bins))


def _psi_counts(expected: Sequence[int], actual: Sequence[int]) -> float | None:
    """PSI from two histograms of counts (every value lands in exactly one bin)."""
    e_total, a_total = sum(expected), sum(actual)
    if not e_total or not a_total:
        return None
    total = 0.0
    for e_count, a_count in zip(expected, actual):
        e_share = max(e_count / e_total, PSI_EPSILON)
        a_share = max(a_count / a_total, PSI_EPSILON)
        total += (a_share - e_share) * math.log(a_share / e_share)
    return total


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


# ------------------------------------------------------------------ reads ---

def stream_rows(ledger: Any, sql: str, params: Sequence[Any] = ()) -> Iterator[tuple[Any, ...]]:
    """Rows as plain tuples in SELECT order, streamed when the ledger supports it.

    ``ReadOnlyLedger.iter_rows`` (dashboard, CLI, operational review, scripts) streams
    from SQLite without one dict per row; any other ledger falls back to ``query``
    (so every selected column needs a distinct name).
    """
    iter_rows = getattr(ledger, "iter_rows", None)
    if callable(iter_rows):
        return iter_rows(sql, params)
    return (tuple(row.values()) for row in ledger.query(sql, params))


# Only the columns the metrics use: never ``state_redacted`` or the exploration distributions.
_DECISION_FIELDS = ("decision_id", "ts", "surface", "mode", "fallback_reason", "latency_ms", "engine_ms",
                    "attempt_count", "tokens_in", "tokens_out", "cost_usd", "legacy_action", "jev_action",
                    "action_taken", "exploration_arm")
_JOIN = "FROM decision_items i JOIN decisions d USING (decision_id) WHERE d.ts >= ? AND d.ts <= ?"
# SQL pre-filter that keeps every row whose ``exposed`` is truthy in Python (Python re-checks).
_MAYBE_EXPOSED = "(i.exposed IS NOT NULL AND i.exposed != 0 AND i.exposed != '')"
_OUTCOMES_SQL = ("SELECT o.decision_id, o.item_ref, o.kind, o.label_source FROM outcomes o JOIN decisions d "
                 "USING (decision_id) WHERE d.ts >= ? AND d.ts <= ?")


def _outcome_sets(ledger: Any, window: tuple[str, str]) -> tuple[set[tuple[str, str, str]], set[tuple[str, str, str]]]:
    """Every ``(decision_id, item_ref, kind)`` outcome of the window's decisions, and the trusted ones."""
    every: set[tuple[str, str, str]] = set()
    trusted: set[tuple[str, str, str]] = set()
    for decision_id, item_ref, kind, label_source in stream_rows(ledger, _OUTCOMES_SQL, window):
        every.add((decision_id, item_ref, kind))
        if label_source not in UNTRUSTED_LABEL_SOURCES:
            trusted.add((decision_id, item_ref, kind))
    return every, trusted


def _calibration_rows(ledger: Any, window: tuple[str, str], mapping: Mapping[str, str]) -> Iterator[tuple[Any, ...]]:
    """Item rows that can enter calibration: calibrated questions, exposed unless the outcome is not a use."""
    wanted = sorted(mapping)
    if not wanted:
        return iter(())
    lifecycle = sorted(question for question, kind in mapping.items() if kind != "used_in_turn")
    exposure = f"({_MAYBE_EXPOSED} OR i.question_id IN ({', '.join('?' for _ in lifecycle)}))" if lifecycle \
        else _MAYBE_EXPOSED
    return stream_rows(
        ledger, "SELECT i.decision_id, i.item_ref, i.question_id, i.question_version, i.answer, i.exposed, d.ts "
        f"{_JOIN} AND i.question_id IN ({', '.join('?' for _ in wanted)}) AND {exposure}",
        (*window, *wanted, *lifecycle))


# ---------------------------------------------------------------- compute ---

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
    mapping = calibration or DEFAULT_CALIBRATION
    decisions = [dict(zip(_DECISION_FIELDS, row)) for row in stream_rows(
        ledger, f"SELECT {', '.join(_DECISION_FIELDS)} FROM decisions WHERE ts >= ? AND ts <= ? ORDER BY ts", window)]
    outcome_set, trusted_set = _outcome_sets(ledger, window)
    by_decision = {row["decision_id"]: row for row in decisions}

    exposures: dict[Any, dict[str, set[tuple[str, str]]]] = defaultdict(lambda: defaultdict(set))
    for decision_id, item_ref, exposed in stream_rows(
            ledger, f"SELECT i.decision_id, i.item_ref, i.exposed {_JOIN} AND {_MAYBE_EXPOSED}", window):
        decision = by_decision.get(decision_id)
        if decision is not None and exposed and item_ref:
            exposures[decision["surface"]][_arm(decision)].add((decision_id, item_ref))

    matured = _CalibrationPass(end, mapping, trusted_set, by_decision)
    for decision_id, item_ref, question_id, version, answer, exposed, ts in _calibration_rows(ledger, window, mapping):
        matured.add(decision_id, item_ref, question_id, f"{question_id}@v{version}", _answer_value(answer), exposed, ts)

    questions, drift = _answer_statistics(ledger, window, end)

    surfaces: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in decisions:
        grouped[row["surface"] or ""].append(row)
    for surface, rows in grouped.items():
        ran = [r for r in rows if not is_skip(r["fallback_reason"])]  # a skip row never ran the engine
        latencies = [r["latency_ms"] for r in ran if (r["attempt_count"] or 0) > 0 and r["latency_ms"] is not None]
        engine = [r["engine_ms"] for r in ran if r["engine_ms"] is not None]  # every decision, sent or not
        with_jev = [r for r in rows if r["jev_action"] is not None]
        breaker_opens = 0
        previous_open = False
        for r in ran:
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
            "exposure_use": _exposure_use(exposures.get(surface, {}), outcome_set),
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
        "questions": questions,
        "calibration": matured.report(),
        "drift": drift,
        "ledger_write_failures": ledger_write_failures(),
        "orphan_send_intents": _orphan_send_intents(ledger, window),
    }


def _orphan_send_intents(ledger: Any, window: tuple[str, str]) -> int | None:
    """Requests sent in the window whose decision row never landed (should stay 0)."""
    try:
        rows = ledger.query(
            "SELECT COUNT(*) AS n FROM send_intents i WHERE i.ts >= ? AND i.ts <= ? AND NOT EXISTS "
            "(SELECT 1 FROM decisions d WHERE d.decision_id = i.decision_id)",
            # An intent younger than a minute is a request still in flight, not an orphan.
            (window[0], min(window[1], utc_iso(datetime.now(timezone.utc) - timedelta(seconds=60)))))
    except Exception:  # swallow-ok: a ledger from before send intents has no table; report unknown
        return None
    return int(rows[0]["n"]) if rows else 0


def _exposure_use(exposed: Mapping[str, set[tuple[str, str]]], outcome_set: set[tuple[str, str, str]]
                  ) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for arm, pairs in exposed.items():
        used = sum(1 for d, ref in pairs if (d, ref, "used_in_turn") in outcome_set)
        result[arm] = {"exposed": len(pairs), "used": used, "rate": used / len(pairs) if pairs else None}
    return result


class _AnswerStats:
    """One question version: answers in the metrics window, and drift histograms of the last two weeks."""

    __slots__ = ("values", "choices", "previous", "current")

    def __init__(self) -> None:
        self.values: list[float] = []
        self.choices: Counter[str] = Counter()
        self.previous: list[int] | None = None
        self.current: list[int] | None = None


def _answer_statistics(ledger: Any, window: tuple[str, str], end: datetime
                       ) -> tuple[dict[str, dict[str, Any]], dict[str, float | None]]:
    """Answer distributions over ``window`` and weekly PSI before ``end``, from one streamed read.

    Only the question, version and answer of each item travel, plus three flags
    computed by SQLite (in the window, in the two-week drift window, in the current
    week), so the rows of a busy week cost a few small objects each.
    """
    until = window[1]
    previous_start, boundary = utc_iso(end - timedelta(days=14)), utc_iso(end - timedelta(days=7))
    by_key: dict[str, _AnswerStats] = {}
    slots: dict[tuple[Any, Any], _AnswerStats] = {}
    rows = stream_rows(
        ledger, "SELECT i.question_id, i.question_version, i.answer, d.ts >= ? AS in_window, "
        "d.ts >= ? AS in_drift, d.ts >= ? AS in_current "
        f"{_JOIN} AND i.question_id != ''",
        (window[0], previous_start, boundary, min(window[0], previous_start), until))
    for question_id, version, answer, in_window, in_drift, in_current in rows:
        stats = slots.get((question_id, version))
        if stats is None:
            key = f"{question_id}@v{version}"
            stats = slots[(question_id, version)] = by_key.setdefault(key, _AnswerStats())
        value = _answer_value(answer)
        if in_window:
            if value is None:
                stats.choices[str(answer)] += 1
            else:
                stats.values.append(value)
        if in_drift and value is not None:
            if in_current:
                counts = stats.current = stats.current or [0] * BINS
            else:
                counts = stats.previous = stats.previous or [0] * BINS
            counts[min(int(value * BINS), BINS - 1)] += 1
    questions: dict[str, dict[str, Any]] = {}
    for key in sorted(by_key):
        stats = by_key[key]
        if not stats.values and not stats.choices:
            continue  # answered only before the window (drift)
        data = stats.values
        questions[key] = {"n": len(data) + sum(stats.choices.values()), "histogram": _histogram(data),
                          "mean": sum(data) / len(data) if data else None, "choices": dict(stats.choices)}
    drift = {key: _psi_counts(by_key[key].previous or [0] * BINS, by_key[key].current)
             for key in sorted(by_key) if by_key[key].current}
    return questions, drift


def calibration_windows(ledger: Any, windows: Sequence[tuple[datetime, datetime]], *,
                        calibration: Mapping[str, str] | None = None) -> list[dict[str, dict[str, Any]]]:
    """``compute_metrics(ledger, since=s, until=u)["calibration"]`` for every ``(s, u)``, from one read.

    The operational review compares consecutive matured weeks per outcome maturity:
    one pass over the union of the windows, limited to the rows that can enter
    calibration, instead of one full ``compute_metrics`` per window.
    """
    mapping = calibration or DEFAULT_CALIBRATION
    spans = [(utc_iso(since), utc_iso(until)) for since, until in windows]
    if not spans:
        return []
    union = (min(since for since, _ in spans), max(until for _, until in spans))
    decisions = {row[0]: dict(zip(("decision_id", "action_taken", "fallback_reason"), row)) for row in stream_rows(
        ledger, "SELECT decision_id, action_taken, fallback_reason FROM decisions WHERE ts >= ? AND ts <= ?", union)}
    _, trusted = _outcome_sets(ledger, union)
    passes = [_CalibrationPass(until, mapping, trusted, decisions) for _, until in windows]
    for decision_id, item_ref, question_id, version, answer, exposed, ts in _calibration_rows(ledger, union, mapping):
        key, value = f"{question_id}@v{version}", _answer_value(answer)
        for (since, until), matured in zip(spans, passes):
            if since <= ts <= until:
                matured.add(decision_id, item_ref, question_id, key, value, exposed, ts)
    return [matured.report() for matured in passes]


class _CalibrationPass:
    """Calibration pairs per question version for one window ending at ``end``.

    An item counts when its question has an outcome kind, its answer is a
    probability, it names an item, it was exposed (for a use outcome), it is not a
    held INGEST candidate (for a lifecycle outcome), its decision is not a skip row
    and its outcome window is over.  Its label is a trusted outcome of that kind.
    """

    def __init__(self, end: datetime, mapping: Mapping[str, str], trusted: set[tuple[str, str, str]],
                 decisions: Mapping[str, Mapping[str, Any]]) -> None:
        self.end = end
        self.mapping = mapping
        self.trusted = trusted
        self.decisions = decisions
        self.pairs: dict[str, list[tuple[float, int]]] = defaultdict(list)
        self.kinds: dict[str, str] = {}
        self._held: dict[str, bool] = {}
        self._times: dict[Any, datetime | None] = {}

    def add(self, decision_id: str, item_ref: str | None, question_id: str, key: str, value: float | None,
            exposed: Any, ts: Any) -> None:
        kind = self.mapping.get(question_id or "")
        if kind is None or value is None or not item_ref:
            return
        if kind == "used_in_turn" and not exposed:
            return  # use can only be observed for exposed items
        decision = self.decisions.get(decision_id)
        if decision is not None and is_skip(decision.get("fallback_reason")):
            return  # the surface did not ask Jev: nothing to calibrate
        if kind not in DEFAULT_MATURITY and self._is_held(decision_id, decision):
            return
        maturity = DEFAULT_MATURITY.get(kind, LIFECYCLE_MATURITY)
        if ts not in self._times:
            self._times[ts] = _parse(ts)
        decided = self._times[ts]
        if decided is None or decided > self.end - maturity:
            return
        self.kinds[key] = kind
        self.pairs[key].append((value, int((decision_id, item_ref, kind) in self.trusted)))

    def _is_held(self, decision_id: str, decision: Mapping[str, Any] | None) -> bool:
        if decision_id not in self._held:
            self._held[decision_id] = _is_held(decision)
        return self._held[decision_id]

    def report(self) -> dict[str, dict[str, Any]]:
        report: dict[str, dict[str, Any]] = {}
        for key, data in self.pairs.items():
            probs, labels = [p for p, _ in data], [y for _, y in data]
            report[key] = {"n": len(data), "positive_kind": self.kinds[key], "base_rate": sum(labels) / len(labels),
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


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def to_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, default=str)


__all__ = [
    "DEFAULT_CALIBRATION",
    "SKIP_PREFIX",
    "brier",
    "calibration_windows",
    "compute_metrics",
    "ece",
    "is_skip",
    "percentile",
    "psi",
    "reliability_bins",
    "stream_rows",
    "to_json",
]
