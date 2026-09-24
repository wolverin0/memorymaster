"""Metrics (health, behaviour, outcomes, calibration, drift) and the training/OPE export."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from memorymaster.decisions import export as ex
from memorymaster.decisions import metrics as mt
from memorymaster.decisions.ledger import DecisionLedger, DecisionRecord, ItemRecord, OutcomeRecord, utc_iso

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------ pure helpers ---

def test_percentile_nearest_rank():
    values = list(range(1, 101))
    assert mt.percentile(values, 50) == 50
    assert mt.percentile(values, 95) == 95
    assert mt.percentile(values, 99) == 99
    assert mt.percentile([], 50) is None
    assert mt.percentile([7], 99) == 7


def test_brier_and_ece_reference_values():
    probs = [0.9, 0.9, 0.1, 0.1]
    labels = [1, 0, 0, 0]
    assert mt.brier(probs, labels) == pytest.approx((0.01 + 0.81 + 0.01 + 0.01) / 4)
    # bin 0.9: acc 0.5 vs conf 0.9 (n=2); bin 0.1: acc 0 vs conf 0.1 (n=2)
    assert mt.ece(probs, labels) == pytest.approx(0.5 * 0.4 + 0.5 * 0.1)
    assert mt.ece([], []) is None and mt.brier([], []) is None
    perfect = mt.reliability_bins([0.05, 0.95], [0, 1])
    assert [b["n"] for b in perfect if b["n"]] == [1, 1]


def test_psi_is_zero_for_identical_and_grows_with_shift():
    base = [i / 100 for i in range(100)]
    assert mt.psi(base, base) == pytest.approx(0.0, abs=1e-9)
    shifted = [min(0.999, v + 0.3) for v in base]
    assert mt.psi(base, shifted) > 0.25
    assert mt.psi([], base) is None


# --------------------------------------------------------- ledger metrics ---

def _d(ledger, did, *, ts, surface="recall", mode="live", fallback=None, outcome="ok", latency=100, cost=0.001,
       legacy='["claim:1"]', jev='["claim:1"]', arm="policy", attempt=1, items=(), tokens=100, engine=None):
    ledger.write_decision(
        DecisionRecord(decision_id=did, ts=utc_iso(ts), surface=surface, mode=mode, fallback_reason=fallback,
                       transport_outcome=outcome, latency_ms=latency, engine_ms=engine, cost_usd=cost,
                       legacy_action=legacy,
                       jev_action=jev, exploration_arm=arm, attempt_count=attempt, tokens_in=tokens, tokens_out=1,
                       action_taken=jev if mode == "live" and not fallback else legacy, chosen_propensity=1.0),
        list(items),
    )


def _item(did, ref, *, q="recall.usable_evidence", answer=0.8, exposed=1):
    return ItemRecord(decision_id=did, item_ref=ref, item_kind="claim", question_id=q, question_version=1,
                      answer=str(answer), probabilities_json=json.dumps({"noul": answer}), exposed=exposed,
                      delivered=exposed)


@pytest.fixture()
def populated(tmp_path):
    ledger = DecisionLedger(tmp_path / "d.db")
    t = NOW - timedelta(hours=3)
    _d(ledger, "a", ts=t, latency=100, items=[_item("a", "claim:1", answer=0.9), _item("a", "claim:2", answer=0.2)])
    _d(ledger, "b", ts=t + timedelta(minutes=1), latency=300, jev='["claim:2"]',
       items=[_item("b", "claim:1", answer=0.7)])
    _d(ledger, "c", ts=t + timedelta(minutes=2), mode="shadow", latency=200, items=[_item("c", "claim:3", answer=0.4)])
    _d(ledger, "e", ts=t + timedelta(minutes=3), arm="explore_order", latency=250,
       items=[_item("e", "claim:4", answer=0.6)])
    _d(ledger, "f1", ts=t + timedelta(minutes=4), mode="shadow", fallback="breaker_open", outcome="timeout",
       latency=900, arm="shadow", jev=None)
    _d(ledger, "f2", ts=t + timedelta(minutes=5), mode="shadow", fallback="breaker_open", outcome="timeout",
       latency=900, arm="shadow", jev=None)
    _d(ledger, "g", ts=t + timedelta(minutes=6), fallback="egress_blocked", outcome="not_sent", attempt=0, cost=0.0,
       latency=None, jev=None, arm="fallback")
    _d(ledger, "h", ts=t + timedelta(minutes=7), fallback="timeout", outcome="timeout", latency=900, jev=None,
       arm="fallback")
    _d(ledger, "old", ts=NOW - timedelta(days=1, hours=2), cost=2.5, surface="ingest", legacy='"admit"',
       jev='"hold"')
    ledger.record_outcomes([
        OutcomeRecord("a", "claim:1", "used_in_turn", 1.0, was_exposed=1, label_source="detector",
                      observed_at=utc_iso(t + timedelta(minutes=10))),
        OutcomeRecord("c", "claim:3", "used_in_turn", 1.0, was_exposed=1, label_source="detector",
                      observed_at=utc_iso(t + timedelta(minutes=10))),
        OutcomeRecord("e", "claim:4", "used_in_turn", 1.0, was_exposed=1, label_source="detector",
                      observed_at=utc_iso(t + timedelta(minutes=10))),
    ])
    return ledger


def test_health_metrics_per_surface(populated):
    report = mt.compute_metrics(populated, since=NOW - timedelta(days=2), until=NOW, daily_usd_cap=2.0)
    recall = report["surfaces"]["recall"]
    assert recall["volume"] == 8
    assert recall["live_pct"] == pytest.approx(3 / 8)
    assert recall["fallback_by_reason"] == {"breaker_open": 2, "egress_blocked": 1, "timeout": 1}
    assert recall["egress_blocked"] == 1
    assert recall["breaker_opens"] == 1
    assert recall["latency_ms"] == {"p50": 300, "p95": 900, "p99": 900, "n": 7}
    assert recall["tokens_in"] == 800 and recall["cost_usd"] == pytest.approx(0.007)
    assert recall["agreement_with_legacy"] == pytest.approx(3 / 4)
    days = {row["day"]: row for row in report["cost_per_day"]}
    assert days["2026-09-22"]["over_cap"] is True and days["2026-09-22"]["cost_usd"] == pytest.approx(2.5)
    assert days["2026-09-23"]["over_cap"] is False
    assert report["ledger_write_failures"] >= 0


def test_engine_overhead_is_reported_next_to_transport_latency(tmp_path):
    ledger = DecisionLedger(tmp_path / "e.db")
    t = NOW - timedelta(hours=1)
    for i, (latency, engine) in enumerate([(100, 130), (200, 260), (None, 5), (900, 950)]):
        _d(ledger, f"x{i}", ts=t + timedelta(minutes=i), latency=latency, engine=engine,
           attempt=0 if latency is None else 1)
    _d(ledger, "unmeasured", ts=t + timedelta(minutes=9), latency=100)
    recall = mt.compute_metrics(ledger, since=NOW - timedelta(days=1), until=NOW)["surfaces"]["recall"]
    # every decision has engine overhead, including the ones that never sent a request
    assert recall["engine_ms"] == {"p50": 130, "p95": 950, "p99": 950, "n": 4}
    assert recall["latency_ms"] == {"p50": 100, "p95": 900, "p99": 900, "n": 4}


def test_exposure_to_use_rates_by_arm(populated):
    report = mt.compute_metrics(populated, since=NOW - timedelta(days=2), until=NOW)
    use = report["surfaces"]["recall"]["exposure_use"]
    assert use["jev"] == {"exposed": 3, "used": 1, "rate": pytest.approx(1 / 3)}
    assert use["legacy"] == {"exposed": 1, "used": 1, "rate": 1.0}
    assert use["explored"] == {"exposed": 1, "used": 1, "rate": 1.0}


def test_question_distributions_and_calibration(populated):
    report = mt.compute_metrics(populated, since=NOW - timedelta(days=2), until=NOW)
    dist = report["questions"]["recall.usable_evidence@v1"]
    assert dist["n"] == 5 and sum(dist["histogram"]) == 5
    assert dist["mean"] == pytest.approx((0.9 + 0.2 + 0.7 + 0.4 + 0.6) / 5)
    cal = report["calibration"]["recall.usable_evidence@v1"]
    assert cal["n"] == 5 and cal["positive_kind"] == "used_in_turn"
    probs, labels = [0.9, 0.2, 0.7, 0.4, 0.6], [1, 0, 0, 1, 1]
    assert cal["brier"] == pytest.approx(mt.brier(probs, labels))
    assert cal["ece"] == pytest.approx(mt.ece(probs, labels))


def test_calibration_skips_held_ingest_candidates_and_untrusted_labels(tmp_path):
    """Same exclusions as ``scripts/jev_calibration_report.py``.

    A held INGEST candidate never reaches the steward, so its missing verdict is not a
    "no"; ``jev`` and ``unattributed_override`` labels are never ground truth.  Without
    this a shift in the hold share moves the weekly ECE the operational review watches.
    """
    ledger = DecisionLedger(tmp_path / "held.db")
    decided = NOW - timedelta(days=10)  # past the 7-day lifecycle maturity
    for did, action, answer in (("admit1", "admit", 0.9), ("admit2", "admit", 0.3), ("held", "hold", 0.2)):
        _d(ledger, did, ts=decided, surface="ingest", legacy='"admit"', jev=json.dumps(action),
           items=[_item(did, f"claim:{did}", q="ingest.usefulness", answer=answer)])
    for did in ("r_jev", "r_override", "r_detector"):
        _d(ledger, did, ts=NOW - timedelta(hours=3), items=[_item(did, f"claim:{did}", answer=0.8)])
    later = utc_iso(NOW - timedelta(hours=2))
    ledger.record_outcomes([
        OutcomeRecord("admit1", "claim:admit1", "steward_confirmed", 1.0, label_source="steward", observed_at=later),
        OutcomeRecord("r_jev", "claim:r_jev", "used_in_turn", 1.0, was_exposed=1, label_source="jev",
                      observed_at=later),
        OutcomeRecord("r_override", "claim:r_override", "used_in_turn", 1.0, was_exposed=1,
                      label_source="unattributed_override", observed_at=later),
        OutcomeRecord("r_detector", "claim:r_detector", "used_in_turn", 1.0, was_exposed=1,
                      label_source="detector", observed_at=later),
    ])
    report = mt.compute_metrics(ledger, since=NOW - timedelta(days=14), until=NOW)
    ingest = report["calibration"]["ingest.usefulness@v1"]
    assert ingest["n"] == 2 and ingest["base_rate"] == pytest.approx(0.5)  # the held candidate is not a negative
    recall = report["calibration"]["recall.usable_evidence@v1"]
    assert recall["n"] == 3 and recall["base_rate"] == pytest.approx(1 / 3)  # only the detector's use counts


def test_skip_rows_count_in_volume_and_fallbacks_only(tmp_path):
    """``record_skip`` rows (``skip:<why>``, attempt_count 0): the surface ran but did not ask Jev.

    They are volume and a fallback reason, never latency or engine time (a skip takes
    ~0 ms and would drag the percentiles down), never a breaker close between two
    open rows, and never calibration (not even an answered item logged on one).
    """
    ledger = DecisionLedger(tmp_path / "skip.db")
    t = NOW - timedelta(hours=3)
    _d(ledger, "asked", ts=t, latency=400, engine=450, items=[_item("asked", "claim:1", answer=0.9)])
    _d(ledger, "open1", ts=t + timedelta(minutes=1), mode="shadow", fallback="breaker_open", outcome="timeout",
       latency=900, engine=950, arm="shadow", jev=None)
    _d(ledger, "skip", ts=t + timedelta(minutes=2), fallback="skip:no_candidates", outcome="not_sent", attempt=0,
       latency=None, engine=1, cost=0.0, tokens=0, jev=None, arm="fallback",
       items=[_item("skip", "claim:2", answer=0.9)])
    _d(ledger, "open2", ts=t + timedelta(minutes=3), mode="shadow", fallback="breaker_open", outcome="timeout",
       latency=900, engine=940, arm="shadow", jev=None)
    later = utc_iso(t + timedelta(minutes=30))
    ledger.record_outcomes([OutcomeRecord(did, ref, "used_in_turn", 1.0, was_exposed=1, label_source="detector",
                                          observed_at=later) for did, ref in (("asked", "claim:1"), ("skip", "claim:2"))])
    report = mt.compute_metrics(ledger, since=NOW - timedelta(days=1), until=NOW)
    recall = report["surfaces"]["recall"]
    assert recall["volume"] == 4
    assert recall["fallback_by_reason"] == {"breaker_open": 2, "skip:no_candidates": 1}
    assert recall["latency_ms"]["n"] == 3 and recall["engine_ms"] == {"p50": 940, "p95": 950, "p99": 950, "n": 3}
    assert recall["breaker_opens"] == 1  # the breaker stayed open across the skip
    assert report["calibration"]["recall.usable_evidence@v1"]["n"] == 1


def test_weekly_drift(tmp_path):
    ledger = DecisionLedger(tmp_path / "d.db")
    for i in range(40):
        _d(ledger, f"p{i}", ts=NOW - timedelta(days=10, minutes=i), items=[_item(f"p{i}", "claim:1", answer=0.1)])
        _d(ledger, f"c{i}", ts=NOW - timedelta(days=2, minutes=i), items=[_item(f"c{i}", "claim:1", answer=0.9)])
    report = mt.compute_metrics(ledger, since=NOW - timedelta(days=14), until=NOW)
    assert report["drift"]["recall.usable_evidence@v1"] > 1.0


def test_empty_ledger_reports_zeroes(tmp_path):
    report = mt.compute_metrics(DecisionLedger(tmp_path / "empty.db"), since=NOW - timedelta(days=1), until=NOW)
    assert report["surfaces"] == {} and report["questions"] == {} and report["cost_per_day"] == []


# ------------------------------------------------------------------ export ---

def test_export_one_row_per_item_with_outcomes_and_split(populated, tmp_path):
    out = tmp_path / "train.jsonl"
    split_at = NOW - timedelta(hours=2, minutes=58, seconds=30)  # between decisions a/b and the rest
    count = ex.export_jsonl(populated, out, since=NOW - timedelta(hours=4), until=NOW, split_at=split_at)
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert count == len(rows)
    by_key = {(r["decision_id"], r["item_ref"]): r for r in rows}
    first = by_key[("a", "claim:1")]
    assert first["answers"]["recall.usable_evidence@v1"]["probabilities"] == {"noul": 0.9}
    assert first["exposed"] == 1 and first["chosen_propensity"] == 1.0
    assert first["outcomes"] == [{"kind": "used_in_turn", "value": 1.0, "label_source": "detector",
                                  "observed_at": first["outcomes"][0]["observed_at"], "lag_s": None,
                                  "was_exposed": 1, "reward_version": None}]
    assert first["label_sources"] == ["detector"]
    assert first["split"] == "train"
    assert by_key[("c", "claim:3")]["split"] == "test"
    assert by_key[("a", "claim:2")]["outcomes"] == []
    assert "state_redacted" not in first
    assert ("old", "") not in by_key  # outside the window


def test_export_includes_state_only_on_request(tmp_path):
    ledger = DecisionLedger(tmp_path / "d.db")
    ledger.write_decision(DecisionRecord(decision_id="s", surface="hints", mode="live", state_redacted='{"x":1}'),
                          [ItemRecord(decision_id="s", item_ref="", question_id="hints.decision", question_version=1,
                                      answer="0.9", probabilities_json='{"noul":0.9}')])
    rows = list(ex.iter_training_rows(ledger, include_state=True))
    assert rows[0]["item_ref"] == "" and rows[0]["state_redacted"] == '{"x":1}'
    assert rows[0]["answers"]["hints.decision@v1"]["answer"] == "0.9"


@pytest.mark.parametrize("source", ["unknown_actor", "unknown"])
def test_unattributed_lifecycle_labels_are_not_ground_truth(tmp_path, source):
    """Ruling R5: a pre-F-21 override (``unknown_actor``) or an unclassifiable event
    (``unknown``) cannot be told apart from an agent acting as operator, so metrics,
    calibration and OPE must all drop it — one shared constant, not three copies."""
    import runpy
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    for name in ("jev_calibration_report.py", "jev_ope.py"):
        assert source in runpy.run_path(str(scripts / name))["UNTRUSTED_SOURCES"]
    ledger = DecisionLedger(tmp_path / "u.db")
    for did in ("trusted", "untrusted"):
        _d(ledger, did, ts=NOW - timedelta(hours=3), items=[_item(did, f"claim:{did}", answer=0.8)])
    later = utc_iso(NOW - timedelta(hours=2))
    ledger.record_outcomes([
        OutcomeRecord("trusted", "claim:trusted", "used_in_turn", 1.0, was_exposed=1, label_source="detector",
                      observed_at=later),
        OutcomeRecord("untrusted", "claim:untrusted", "used_in_turn", 1.0, was_exposed=1, label_source=source,
                      observed_at=later),
    ])
    report = mt.compute_metrics(ledger, since=NOW - timedelta(days=1), until=NOW)
    recall = report["calibration"]["recall.usable_evidence@v1"]
    assert recall["n"] == 2 and recall["base_rate"] == pytest.approx(0.5)  # the unattributed use is no positive


def test_metrics_report_requests_sent_without_a_decision_row(tmp_path):
    ledger = DecisionLedger(tmp_path / "orphans.db")
    _d(ledger, "kept", ts=NOW - timedelta(hours=2), items=[_item("kept", "claim:1", answer=0.8)])
    assert ledger.reserve_send("kept", "recall", ts=utc_iso(NOW - timedelta(hours=2)), est_cost_usd=0.001)
    assert ledger.reserve_send("lost", "recall", ts=utc_iso(NOW - timedelta(hours=1)), est_cost_usd=0.001)
    report = mt.compute_metrics(ledger, since=NOW - timedelta(days=1), until=NOW)
    assert report["orphan_send_intents"] == 1
