"""Calibration report for Jev questions: reliability, ECE, Brier and a certified threshold fit.

Per question version (``id@vN``) it pairs each numeric answer with a label from
exactly one source, never a mix:

* a question with an outcome kind in ``decisions.metrics.DEFAULT_CALIBRATION``
  (e.g. ``recall.usable_evidence`` -> ``used_in_turn``) uses that outcome only:
  counted for exposed items when the outcome is a use, only after the outcome's
  maturity window, never from ``jev`` or ``unattributed_override`` label sources,
  and never for a ``held`` INGEST candidate (it cannot be confirmed while held).
  Items of a ``skip:`` decision (the surface did not ask Jev) never count.
  Operator labels on such a question are counted in ``operator_labels_excluded``;
* any other question uses operator reviews (dashboard Correct/Incorrect,
  ``operator_review`` outcome; the latest ``truth`` wins).  The review queue picks
  items for information (near threshold, disagreements), so such an entry carries
  a ``sample_warning``: its ECE and threshold fit describe hard cases.

Threshold fit (split-half by time): on the earlier half, the smallest grid
threshold whose precision (share of positives among answers >= t) has a one-sided
95 % Wilson lower bound >= ``--target-precision``; it is recommended only when the
later half certifies it the same way, and never with fewer than 100 labelled
outcomes.  The ledger is opened read-only.

    python scripts/jev_calibration_report.py --target-precision 0.85
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

Z_ONE_SIDED_95 = 1.6448536269514722
MIN_OUTCOMES = 100
MIN_SUPPORT = 10
DEFAULT_TARGET_PRECISION = 0.8
GRID = tuple(round(0.05 * step, 2) for step in range(1, 20))
from memorymaster.decisions.metrics import SKIP_PREFIX, UNTRUSTED_LABEL_SOURCES  # noqa: E402 — after sys.path bootstrap

UNTRUSTED_SOURCES = UNTRUSTED_LABEL_SOURCES
REVIEW_KIND = "operator_review"
ASKED_JEV = f"NOT COALESCE(d.fallback_reason GLOB '{SKIP_PREFIX}*', 0)"  # a skip: row never asked Jev
HELD_ACTION = "hold"
OPERATOR_SAMPLE_WARNING = ("operator review labels: items the review queue picked for information (near a "
                           "threshold, disagreements), not a random sample; ECE and the threshold fit are biased "
                           "toward hard cases")


def open_ledger(path: str | Path | None = None):
    from memorymaster.surfaces.jev_review import read_ledger

    return read_ledger(path)


def wilson_lower(successes: int, n: int, z: float = Z_ONE_SIDED_95) -> float:
    """One-sided Wilson score lower bound for a proportion (0.0 when there is no data)."""
    if n <= 0:
        return 0.0
    p = successes / n
    denominator = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denominator)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None


def load_pairs(ledger: Any, *, now: datetime, since: datetime | None = None,
               calibration: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """``{"id@vN": {"pairs": [(ts, p, y)], "sources": Counter, "kind": str, ...}}`` in decision-time order."""
    from memorymaster.decisions.ledger import utc_iso
    from memorymaster.decisions.metrics import DEFAULT_CALIBRATION, DEFAULT_MATURITY, LIFECYCLE_MATURITY

    mapping = calibration or DEFAULT_CALIBRATION
    clause, params = "d.ts <= ?", [utc_iso(now)]
    if since is not None:
        clause += " AND d.ts >= ?"
        params.append(utc_iso(since))
    items = ledger.query(
        "SELECT i.decision_id, i.item_ref, i.question_id, i.question_version, i.answer, i.exposed, d.ts, "
        "d.action_taken "
        f"FROM decision_items i JOIN decisions d USING (decision_id) WHERE i.question_id != '' AND {clause} "
        f"AND {ASKED_JEV} ORDER BY d.ts, i.decision_id, i.item_ref, i.question_id", params)
    outcomes = ledger.query(
        "SELECT o.decision_id, o.item_ref, o.kind, o.label_source, o.details_json FROM outcomes o "
        f"JOIN decisions d USING (decision_id) WHERE {clause} ORDER BY o.observed_at, o.outcome_id", params)
    trusted: set[tuple[str, str, str]] = set()
    operator: dict[tuple[str, str, str], int] = {}
    for row in outcomes:
        if row["kind"] == REVIEW_KIND:
            if row["label_source"] != "operator":
                continue
            try:
                details = json.loads(row["details_json"] or "{}")
            except ValueError:
                continue
            truth = details.get("truth") if isinstance(details, dict) else None
            if truth in (0, 1):
                operator[(row["decision_id"], row["item_ref"], str(details.get("question_id")))] = int(truth)
        elif row["label_source"] not in UNTRUSTED_SOURCES:
            trusted.add((row["decision_id"], row["item_ref"], row["kind"]))
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"pairs": [], "sources": Counter(), "kind": None, "operator_excluded": 0, "held_excluded": 0})
    for row in items:
        p = _number(row["answer"])
        if p is None:
            continue
        key = f"{row['question_id']}@v{row['question_version']}"
        truth = operator.get((row["decision_id"], row["item_ref"], row["question_id"]))
        kind = mapping.get(row["question_id"])
        if kind is not None:  # outcome-calibrated: an information-selected operator label is never mixed in
            if truth is not None:
                grouped[key]["operator_excluded"] += 1
            if kind == "used_in_turn" and not row["exposed"]:
                continue  # use is only observable for exposed items
            if kind not in DEFAULT_MATURITY and _action(row["action_taken"]) == HELD_ACTION:
                grouped[key]["held_excluded"] += 1
                continue  # a held candidate never reaches the steward: no verdict is not a "no"
            decided = _parse(row["ts"])
            if decided is None or decided > now - DEFAULT_MATURITY.get(kind, LIFECYCLE_MATURITY):
                continue  # outcome window not over yet
            label, source = int((row["decision_id"], row["item_ref"], kind) in trusted), f"outcome:{kind}"
        elif truth is not None:
            label, source = truth, "operator"
        else:
            continue
        entry = grouped[key]
        entry["pairs"].append((row["ts"], p, label))
        entry["sources"][source] += 1
        entry["kind"] = entry["kind"] or kind or REVIEW_KIND
    return {key: entry for key, entry in grouped.items() if entry["pairs"]}


def _action(value: Any) -> Any:
    try:
        return json.loads(value) if value is not None else None
    except (TypeError, ValueError):
        return value


def fit_threshold(pairs: list[tuple[str, float, int]], *, target: float = DEFAULT_TARGET_PRECISION,
                  min_outcomes: int = MIN_OUTCOMES) -> dict[str, Any]:
    method = ("split-half by time: fit on the earlier half, certify on the later half; smallest grid threshold "
              f"whose precision at answer >= t has a one-sided 95% Wilson lower bound >= {target}")
    result: dict[str, Any] = {"method": method, "target_precision": target, "min_outcomes": min_outcomes,
                              "grid": list(GRID), "n": len(pairs), "fit": None, "validation": None,
                              "recommended": None}
    if len(pairs) < min_outcomes:
        return {**result, "reason": f"insufficient outcomes: {len(pairs)} < {min_outcomes}; no recommendation"}
    ordered = sorted(pairs, key=lambda pair: pair[0])
    half = len(ordered) // 2
    fit, later = ordered[:half], ordered[half:]

    def at(sample: list[tuple[str, float, int]], threshold: float) -> dict[str, Any]:
        selected = [label for _, p, label in sample if p >= threshold]
        hits = sum(selected)
        return {"n": len(sample), "threshold": threshold, "support": len(selected),
                "precision": hits / len(selected) if selected else None,
                "lcb95": wilson_lower(hits, len(selected))}

    chosen = None
    for threshold in GRID:
        stats = at(fit, threshold)
        if stats["support"] >= MIN_SUPPORT and stats["lcb95"] >= target:
            chosen = stats
            break
    if chosen is None:
        return {**result, "fit": {"n": len(fit), "threshold": None}, "validation": {"n": len(later)},
                "reason": "no grid threshold reaches the target precision on the earlier half"}
    validation = at(later, chosen["threshold"])
    certified = validation["support"] >= MIN_SUPPORT and validation["lcb95"] >= target
    return {**result, "fit": chosen, "validation": validation,
            "recommended": chosen["threshold"] if certified else None,
            "reason": "certified on the later half" if certified else "not certified on the later half"}


def _current_thresholds(ledger: Any) -> dict[str, Any]:
    rows = ledger.query("SELECT question_id, version, threshold_json FROM question_versions")
    current: dict[str, Any] = {}
    for row in rows:
        try:
            current[f"{row['question_id']}@v{row['version']}"] = json.loads(row["threshold_json"] or "{}")
        except ValueError:
            continue
    return current


def build_report(ledger: Any, *, now: datetime | None = None, since: datetime | None = None,
                 target_precision: float = DEFAULT_TARGET_PRECISION) -> dict[str, Any]:
    from memorymaster.decisions.metrics import brier, ece, reliability_bins

    end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    grouped = load_pairs(ledger, now=end, since=since)
    current = _current_thresholds(ledger) if ledger.exists() else {}
    questions: dict[str, Any] = {}
    for key in sorted(grouped):
        entry = grouped[key]
        probs = [p for _, p, _ in entry["pairs"]]
        labels = [y for _, _, y in entry["pairs"]]
        questions[key] = {
            "n": len(labels), "positives": sum(labels), "base_rate": sum(labels) / len(labels),
            "positive_kind": entry["kind"], "label_sources": dict(entry["sources"]),
            "operator_labels_excluded": entry["operator_excluded"], "held_excluded": entry["held_excluded"],
            "sample_warning": OPERATOR_SAMPLE_WARNING if entry["sources"].get("operator") else None,
            "ece": ece(probs, labels), "brier": brier(probs, labels), "bins": reliability_bins(probs, labels),
            "current_thresholds": current.get(key),
            "threshold_fit": fit_threshold(entry["pairs"], target=target_precision),
        }
    return {"generated_at": end.isoformat(), "since": since.isoformat() if since else None,
            "target_precision": target_precision, "min_outcomes": MIN_OUTCOMES, "questions": questions}


def _time(value: str) -> datetime:
    raw = value.strip()
    parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _target(value: str) -> float:
    number = float(value)
    if not 0.0 < number < 1.0:
        raise argparse.ArgumentTypeError("target precision must be between 0 and 1")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=None, help="decisions ledger (default: MEMORYMASTER_DECISIONS_DB)")
    parser.add_argument("--since", type=_time, default=None, help="ISO-8601 lower bound on decision time")
    parser.add_argument("--now", type=_time, default=None, help="evaluation time (default: now)")
    parser.add_argument("--target-precision", type=_target, default=DEFAULT_TARGET_PRECISION)
    args = parser.parse_args(argv)
    report = build_report(open_ledger(args.db), now=args.now, since=args.since,
                          target_precision=args.target_precision)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
