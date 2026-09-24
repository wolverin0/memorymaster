"""Learning scripts over synthetic decision ledgers: calibration report, off-policy evaluation, export.

* ``jev_calibration_report``: per question version reliability bins, ECE and Brier;
  a split-half threshold fit whose recommendation needs a one-sided 95 % Wilson
  lower bound on the later half, and never below 100 labelled outcomes.
* ``jev_ope``: IPS, SNIPS and doubly robust estimates of a candidate threshold or
  top-k policy against the logging policy, always reporting n, effective sample
  size, the clipping rule and a bootstrap interval.
* ``jev_export``: ``decisions.export`` JSONL with the time-split flag.
"""
from __future__ import annotations

import json
import random
import runpy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.decisions.ledger import DecisionLedger, DecisionRecord, ItemRecord, OutcomeRecord, utc_iso
from scripts import jev_calibration_report as cal
from scripts import jev_export as exp
from scripts import jev_ope as ope

Q = runpy.run_path(str(Path(__file__).with_name("test_jev_review_queue.py")))
decision, item = Q["decision"], Q["item"]
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _used(ledger, did, ref, at, *, source="detector", kind="used_in_turn"):
    ledger.record_outcomes([OutcomeRecord(did, ref, kind, 1.0, was_exposed=1, label_source=source,
                                          observed_at=utc_iso(at))])


def _gapped(rng):
    """Positives score in [0.6, 1), negatives below 0.19 (0.2 after rounding): full precision from 0.2 up."""
    return rng.uniform(0.6, 1.0) if rng.random() < 0.5 else rng.uniform(0.0, 0.19)


def _calibration_ledger(tmp_path, n, *, label=lambda p: int(p >= 0.6), sample=_gapped, seed=7):
    ledger = DecisionLedger(tmp_path / "cal.db")
    rng = random.Random(seed)
    start = NOW - timedelta(days=20)
    for index in range(n):
        p = round(sample(rng), 3)
        did, ref, ts = f"c{index:04d}", f"claim:{index}", start + timedelta(minutes=10 * index)
        decision(ledger, did, ts=ts, items=[item(did, ref, "recall.usable_evidence", p)])
        if label(p):
            _used(ledger, did, ref, ts + timedelta(minutes=1))
    return ledger


# --------------------------------------------------------------- calibration ---

def test_calibration_report_bins_ece_brier_and_a_confirmed_threshold(tmp_path):
    ledger = _calibration_ledger(tmp_path, 240)
    report = cal.build_report(cal.open_ledger(ledger.path), now=NOW, target_precision=0.8)
    entry = report["questions"]["recall.usable_evidence@v1"]
    assert entry["n"] == 240 and entry["positive_kind"] == "used_in_turn"
    assert len(entry["bins"]) == 10 and sum(b["n"] for b in entry["bins"]) == 240
    assert 0.0 <= entry["ece"] <= 1.0 and 0.0 <= entry["brier"] <= 1.0
    fit = entry["threshold_fit"]
    assert fit["method"].startswith("split-half")
    assert fit["fit"]["n"] == 120 and fit["validation"]["n"] == 120
    # Smallest grid threshold whose precision is certified: 0.15 still admits negatives.
    assert fit["recommended"] == pytest.approx(0.2)
    assert fit["fit"]["threshold"] == pytest.approx(0.2)
    assert fit["validation"]["precision"] == 1.0 and fit["validation"]["lcb95"] >= 0.8


def test_calibration_refuses_to_recommend_below_100_outcomes(tmp_path):
    ledger = _calibration_ledger(tmp_path, 60)
    entry = cal.build_report(cal.open_ledger(ledger.path), now=NOW)["questions"]["recall.usable_evidence@v1"]
    assert entry["n"] == 60 and entry["ece"] is not None
    assert entry["threshold_fit"]["recommended"] is None
    assert "100" in entry["threshold_fit"]["reason"]


def test_calibration_uses_operator_truth_and_ignores_jev_labels(tmp_path):
    ledger = DecisionLedger(tmp_path / "cal.db")
    ts = NOW - timedelta(days=3)
    decision(ledger, "a", ts=ts, surface="revalidate",
             items=[item("a", "claim:1", "lifecycle.still_valid", 0.9)])
    decision(ledger, "b", ts=ts, items=[item("b", "claim:2", "recall.usable_evidence", 0.9)])
    _used(ledger, "b", "claim:2", ts + timedelta(minutes=1), source="jev")  # Jev never grades itself
    ledger.record_outcomes([OutcomeRecord(
        "a", "claim:1", "operator_review", 0.0, label_source="operator", observed_at=utc_iso(ts + timedelta(hours=1)),
        details_json=json.dumps({"question_id": "lifecycle.still_valid", "question_version": 1,
                                 "verdict": "incorrect", "answer": 0.9, "truth": 0}))])
    report = cal.build_report(cal.open_ledger(ledger.path), now=NOW)["questions"]
    assert report["lifecycle.still_valid@v1"]["n"] == 1
    assert report["lifecycle.still_valid@v1"]["label_sources"] == {"operator": 1}
    assert report["lifecycle.still_valid@v1"]["base_rate"] == 0.0
    assert report["recall.usable_evidence@v1"]["base_rate"] == 0.0


def test_calibration_never_mixes_operator_labels_into_outcome_calibrated_questions(tmp_path):
    """Operator labels come from an information-selected queue: never mixed with the outcome sample."""
    ledger = DecisionLedger(tmp_path / "cal.db")
    ts = NOW - timedelta(days=3)
    decision(ledger, "u", ts=ts, items=[item("u", "claim:1", "recall.usable_evidence", 0.9)])
    _used(ledger, "u", "claim:1", ts + timedelta(minutes=1))
    decision(ledger, "hidden", ts=ts, items=[item("hidden", "claim:2", "recall.usable_evidence", 0.9, exposed=0)])
    for did, ref, truth in (("u", "claim:1", 0), ("hidden", "claim:2", 1)):  # near-threshold picks, say
        ledger.record_outcomes([OutcomeRecord(
            did, ref, "operator_review", float(truth), label_source="operator",
            observed_at=utc_iso(ts + timedelta(hours=2)),
            details_json=json.dumps({"question_id": "recall.usable_evidence", "question_version": 1,
                                     "verdict": "correct" if truth else "incorrect", "answer": 0.9,
                                     "truth": truth}))])
    decision(ledger, "s", ts=ts, surface="revalidate", items=[item("s", "claim:3", "lifecycle.still_valid", 0.7)])
    ledger.record_outcomes([OutcomeRecord(
        "s", "claim:3", "operator_review", 1.0, label_source="operator", observed_at=utc_iso(ts + timedelta(hours=2)),
        details_json=json.dumps({"question_id": "lifecycle.still_valid", "question_version": 1,
                                 "verdict": "correct", "answer": 0.7, "truth": 1}))])
    report = cal.build_report(cal.open_ledger(ledger.path), now=NOW)["questions"]
    usable = report["recall.usable_evidence@v1"]
    assert usable["n"] == 1 and usable["positives"] == 1  # the observed use, not the operator's 0
    assert usable["label_sources"] == {"outcome:used_in_turn": 1}
    assert usable["operator_labels_excluded"] == 2
    assert usable["sample_warning"] is None
    still = report["lifecycle.still_valid@v1"]
    assert still["label_sources"] == {"operator": 1}
    assert "not a random sample" in still["sample_warning"]


def test_calibration_does_not_count_held_ingest_candidates_as_unconfirmed(tmp_path):
    """A held candidate never reaches the steward: no steward_confirmed is not a negative label."""
    ledger = DecisionLedger(tmp_path / "cal.db")
    ts = NOW - timedelta(days=10)  # past the 7-day lifecycle maturity
    for index, action in enumerate(("admit", "admit", "hold", "hold", "hold")):
        did = f"g{index}"
        decision(ledger, did, ts=ts + timedelta(minutes=index), surface="ingest", legacy='"admit"',
                 jev=json.dumps(action), items=[item(did, f"cand:{index}", "ingest.usefulness", 0.9)])
    _used(ledger, "g0", "cand:0", ts + timedelta(days=1), source="steward", kind="steward_confirmed")
    entry = cal.build_report(cal.open_ledger(ledger.path), now=NOW)["questions"]["ingest.usefulness@v1"]
    assert entry["n"] == 2 and entry["positives"] == 1
    assert entry["held_excluded"] == 3


def test_calibration_report_ignores_skip_rows(tmp_path):
    """A ``skip:`` decision did not ask Jev: an item logged on one is never calibration evidence."""
    ledger = DecisionLedger(tmp_path / "cal.db")
    ts = NOW - timedelta(days=3)
    decision(ledger, "asked", ts=ts, items=[item("asked", "claim:1", "recall.usable_evidence", 0.9)])
    decision(ledger, "skipped", ts=ts, fallback="skip:no_candidates", outcome="not_sent", attempt=0, jev=None,
             items=[item("skipped", "claim:2", "recall.usable_evidence", 0.9)])
    _used(ledger, "asked", "claim:1", ts + timedelta(minutes=1))
    _used(ledger, "skipped", "claim:2", ts + timedelta(minutes=1))
    entry = cal.build_report(cal.open_ledger(ledger.path), now=NOW)["questions"]["recall.usable_evidence@v1"]
    assert entry["n"] == 1 and entry["positives"] == 1


def test_wilson_lower_bound_reference_values():
    assert cal.wilson_lower(0, 0) == 0.0
    assert cal.wilson_lower(50, 50) == pytest.approx(50 / (50 + 1.6448536 ** 2), rel=1e-6)
    assert cal.wilson_lower(80, 100) == pytest.approx(0.7266, abs=1e-3)


def test_calibration_cli_prints_json(tmp_path, capsys):
    ledger = _calibration_ledger(tmp_path, 30)
    assert cal.main(["--db", str(ledger.path), "--now", NOW.isoformat()]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["questions"]["recall.usable_evidence@v1"]["threshold_fit"]["recommended"] is None


# ----------------------------------------------------------------------- OPE ---

def _ope_ledger(tmp_path, n=600, seed=3):
    """Ingest decisions logged by a stochastic policy with known propensities.

    Gate g ~ U(0,1).  Logging policy admits with p=0.7, holds with p=0.3.  An
    admitted candidate is later steward-confirmed iff g >= 0.5; a held one never is.
    True value of "admit iff g >= 0.5": P(g >= 0.5) ~ 0.5.  Logging value: 0.35.
    """
    ledger = DecisionLedger(tmp_path / "ope.db")
    rng = random.Random(seed)
    start = NOW - timedelta(days=10)
    truth = 0
    for index in range(n):
        g = rng.random()
        truth += g >= 0.5
        admitted = rng.random() < 0.7
        action = "admit" if admitted else "hold"
        did, ref, ts = f"o{index:04d}", f"claim:{index}", start + timedelta(minutes=index)
        ledger.write_decision(DecisionRecord(
            decision_id=did, ts=utc_iso(ts), surface="ingest", mode="live", transport_outcome="ok",
            legacy_action=json.dumps("admit"), jev_action=json.dumps(action), action_taken=json.dumps(action),
            available_actions_json=json.dumps(["admit", "hold"]),
            action_propensities_json=json.dumps([{"action": "admit", "p": 0.7}, {"action": "hold", "p": 0.3}]),
            chosen_propensity=0.7 if admitted else 0.3, exploration_arm="policy" if admitted else "explore_hold",
        ), [ItemRecord(decision_id=did, item_ref=ref, item_kind="claim", question_id=f"ingest.{check}",
                       question_version=1, answer=str(round(g if check == "evidence" else 0.99, 4)))
            for check in ("evidence", "usefulness")])
        if admitted and g >= 0.5:
            _used(ledger, did, ref, ts + timedelta(hours=1), source="steward", kind="steward_confirmed")
    return ledger, truth / n


def test_ope_threshold_policy_recovers_the_true_value(tmp_path):
    ledger, true_value = _ope_ledger(tmp_path)
    result = ope.evaluate(ope.open_ledger(ledger.path), surface="ingest", policy="threshold",
                          questions=["ingest.evidence", "ingest.usefulness"], threshold=0.5,
                          above="admit", below="hold", reward_kind="steward_confirmed", clip=10.0,
                          bootstrap=300, seed=1, now=NOW)
    assert result["n"] == 600
    assert result["support"] == {"supported": 600, "unsupported": 0, "unverified": 0, "coverage": 1.0}
    assert result["logging_value"] == pytest.approx(0.35, abs=0.06)
    for name in ("ips", "snips", "dr"):
        estimate = result["estimates"][name]
        low, high = estimate["ci95"]
        assert low <= true_value <= high, (name, estimate)
        assert estimate["value"] == pytest.approx(true_value, abs=0.08)
    assert 0 < result["ess"] <= result["n"]
    assert result["clipping"]["rule"] == "w = min(pi/mu, 10.0)"
    assert result["bootstrap"] == {"replicates": 300, "seed": 1, "method": "percentile", "level": 0.95}


def test_ope_clipping_and_effective_sample_size(tmp_path):
    ledger, _ = _ope_ledger(tmp_path, n=200)
    result = ope.evaluate(ope.open_ledger(ledger.path), surface="ingest", policy="threshold",
                          questions=["ingest.evidence"], threshold=0.5, above="hold", below="hold",
                          reward_kind="steward_confirmed", clip=2.0, bootstrap=50, seed=0, now=NOW)
    # Always "hold": matched decisions have w = 1/0.3 = 3.33, clipped to 2.0.
    assert result["clipping"]["clipped"] == result["n_matched"] > 0
    assert result["ess"] == pytest.approx(result["n_matched"])  # equal weights: ESS = number of non-zero weights
    assert result["estimates"]["ips"]["value"] == 0.0


def test_ope_topk_policy_matches_logged_rankings(tmp_path):
    ledger = DecisionLedger(tmp_path / "rank.db")
    for index in range(40):
        did, ts = f"r{index}", NOW - timedelta(hours=40 - index)
        order = ["claim:a", "claim:b"] if index % 2 == 0 else ["claim:b", "claim:a"]
        ledger.write_decision(DecisionRecord(
            decision_id=did, ts=utc_iso(ts), surface="recall", mode="live", transport_outcome="ok",
            legacy_action=json.dumps(["claim:a", "claim:b"]), jev_action=json.dumps(order),
            action_taken=json.dumps(order), chosen_propensity=0.5,
        ), [ItemRecord(decision_id=did, item_ref=ref, item_kind="claim", question_id=q, question_version=1,
                       answer=str(value), exposed=1, delivered=1)
            for ref, q, value in (("claim:a", "recall.relevant", 0.9), ("claim:b", "recall.relevant", 0.4),
                                  ("claim:a", "recall.usable_evidence", 0.9),
                                  ("claim:b", "recall.usable_evidence", 0.9))])
        _used(ledger, did, order[0], ts + timedelta(minutes=2))  # the first item is always the one used
    result = ope.evaluate(ope.open_ledger(ledger.path), surface="recall", policy="topk",
                          questions=["recall.relevant"], include_question="recall.usable_evidence", threshold=0.5,
                          k_min=2, k_max=5, reward_kind="used_in_turn", clip=10.0, bootstrap=100, seed=0, now=NOW)
    assert result["n"] == 40 and result["n_matched"] == 20
    # No logged distribution: the 20 other orderings cannot be checked for support, and say so.
    assert result["support"] == {"supported": 20, "unsupported": 0, "unverified": 20, "coverage": 0.5}
    assert any("could not be verified" in warning for warning in result["warnings"]), result["warnings"]
    assert result["estimates"]["ips"]["value"] == pytest.approx(1.0)
    assert result["estimates"]["snips"]["value"] == pytest.approx(1.0)


def test_ope_topk_propensity_is_the_prefix_marginal_of_the_logged_orderings():
    """Ranking exploration logs a distribution over whole top-5 orderings; the action is only its first k refs."""
    orderings = [["a", "b", "c"], ["a", "c", "b"], ["b", "a", "c"], ["b", "c", "a"], ["c", "a", "b"], ["c", "b", "a"]]
    probabilities = [0.5, 0.2, 0.1, 0.1, 0.06, 0.04]
    logged = json.dumps([{"action": order, "p": p} for order, p in zip(orderings, probabilities)])
    assert ope.logging_propensity(["a", "b"], logged, 0.5) == pytest.approx(0.5)
    assert ope.logging_propensity(["a"], logged, 0.5) == pytest.approx(0.7)  # every ordering starting with a
    assert ope.logging_propensity(["b", "c"], logged, 0.1) == pytest.approx(0.1)
    assert ope.logging_propensity(["c", "a", "b"], logged, 0.06) == pytest.approx(0.06)  # k = 5: whole ordering
    assert ope.logging_propensity(["a", "b", "c", "rest:1"], logged, 0.5) == pytest.approx(0.5)  # k > 5: fixed tail
    # Binary exploration and fallback rows: the action's own logged probability.
    binary = json.dumps([{"action": ["x", "y"], "p": 0.95}, {"action": ["x", "y", "z"], "p": 0.05}])
    assert ope.logging_propensity(["x", "y"], binary, 0.95) == pytest.approx(0.95)
    assert ope.logging_propensity("hold", json.dumps([{"action": "admit", "p": 0.7}, {"action": "hold", "p": 0.3}]),
                                  0.3) == pytest.approx(0.3)
    # Nothing usable logged: the chosen propensity is all there is.
    assert ope.logging_propensity(["a", "b"], None, 0.5) == 0.5
    assert ope.logging_propensity(["a", "b"], "not json", 0.5) == 0.5
    assert ope.logging_propensity(["q", "r"], logged, 0.5) == 0.5


def test_ope_candidate_action_has_no_logging_support_outside_the_logged_k_and_top():
    """Ranking exploration permutes only the explored top refs; k and the tail are deterministic."""
    orderings = [["a", "b", "c"], ["a", "c", "b"], ["b", "a", "c"], ["b", "c", "a"], ["c", "a", "b"], ["c", "b", "a"]]
    logged = json.dumps([{"action": order, "p": p} for order, p in zip(orderings, [0.5, 0.2, 0.1, 0.1, 0.06, 0.04])])
    # Jev's k was 2 (logged action b, c): any other top 2 of the explored refs is supported ...
    assert ope.action_probability(["a", "b"], ["b", "c"], logged) == pytest.approx(0.5)
    assert ope.action_probability(["c", "a"], ["b", "c"], logged) == pytest.approx(0.06)
    # ... but another k, or a ref outside the explored top, never was.
    assert ope.action_probability(["a"], ["b", "c"], logged) == 0.0
    assert ope.action_probability(["a", "b", "c"], ["b", "c"], logged) == 0.0
    assert ope.action_probability(["a", "x"], ["b", "c"], logged) == 0.0
    # k beyond the explored top: the tail is fixed by the logged action.
    assert ope.action_probability(["b", "a", "c", "d"], ["a", "b", "c", "d"], logged) == pytest.approx(0.1)
    assert ope.action_probability(["b", "a", "c", "e"], ["a", "b", "c", "d"], logged) == 0.0
    # Fallback / shadow rows log the legacy action with p = 1: nothing else is supported.
    fallback = json.dumps([{"action": ["x", "y"], "p": 1.0}])
    assert ope.action_probability(["x", "y"], ["x", "y"], fallback) == 1.0
    assert ope.action_probability(["y", "x"], ["x", "y"], fallback) == 0.0
    assert ope.action_probability("hold", "admit", json.dumps([{"action": "admit", "p": 1.0}])) == 0.0
    binary = json.dumps([{"action": "admit", "p": 0.7}, {"action": "hold", "p": 0.3}])
    assert ope.action_probability("hold", "admit", binary) == pytest.approx(0.3)
    # No logged distribution: support cannot be checked.
    assert ope.action_probability(["a", "b"], ["a", "b"], None) is None
    assert ope.action_probability(["a", "b"], ["a", "b"], "not json") is None


class _RankingTransport:
    """Fresh seeded answers per request: relevance score and usable-evidence noul for every candidate."""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.calls: list[dict[str, tuple[float, float]]] = []

    def send(self, payload, *, expected, timeout_s, max_retries=0):
        from memorymaster.decisions.transport import ParsedAnswer, TransportResult

        values: dict[str, tuple[float, float]] = {}
        answers = {}
        for wire_id, schema in expected.items():
            ref = wire_id.split("::", 1)[1]
            relevant, usable = values.setdefault(ref, (self.rng.random(), self.rng.random()))
            if schema.primitive == "score":
                answers[wire_id] = ParsedAnswer("score", relevant, {"1": 1.0 - relevant, "4": relevant}, None, "4")
            else:
                value = usable if wire_id.startswith("recall.usable_evidence") else 0.1
                answers[wire_id] = ParsedAnswer("noul", value, {"noul": value})
        self.calls.append(values)
        return TransportResult(200, {"model": "jev-1.13.0"}, 40, 1, "ok", model_served="jev-1.13.0",
                               tokens_in=500, tokens_out=3, cost_usd=500 * 0.042e-6, answers=answers)

    def close(self):
        pass


RECALL_REFS = [f"claim:{index}" for index in range(7)]


class _Clock:
    """Engine clock the transport can push past the hook deadline: the answer arrives, but late."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _engine_logged_recall(tmp_path, *, n=300, seed=11, late_every=None):
    """Real engine, recall live with 30 % Plackett-Luce exploration over the top 5 and adaptive k in [2, 5].

    Jev's policy orders by relevance and takes k = number of candidates whose usable
    evidence is >= 0.5, clamped to [2, 5].  A delivered candidate is used iff its
    relevance is >= 0.7.  Returns the ledger path and, per decision, the decision and
    the transport's (relevance, usable) values, so any candidate policy's true value
    can be computed.  With ``late_every=k`` every k-th answer arrives after the hook
    deadline: the engine logs it as a ``late`` fallback that took the legacy action.
    """
    from memorymaster.decisions import questions as q
    from memorymaster.decisions.config import DecisionConfig
    from memorymaster.decisions.credentials import ApiKey
    from memorymaster.decisions.engine import DecisionContext, DecisionEngine, DecisionItem, JevChoice

    def choose(answers) -> JevChoice:
        refs = sorted(answers.item_refs(), key=lambda r: -(answers.score("recall.relevant", r) or 0.0))
        k = max(2, min(5, sum(1 for r in refs if (answers.noul("recall.usable_evidence", r) or 0) >= 0.5)))
        return JevChoice(order=refs, k=k, scores={r: answers.score("recall.relevant", r) or 0.0 for r in refs})

    db = tmp_path / "decisions.db"
    config = DecisionConfig.from_env({"MEMORYMASTER_JEV_MODE": "live", "MEMORYMASTER_JEV_EXPLORE_RECALL": "0.3",
                                      "MEMORYMASTER_JEV_RPM_CAP": "100000", "MEMORYMASTER_DECISIONS_DB": str(db)})
    transport = _RankingTransport(seed=seed)
    clock = _Clock()
    if late_every:
        answer = transport.send

        def send(payload, **kwargs):
            if (len(transport.calls) + 1) % late_every == 0:
                clock.now += 5.0  # past the 900 ms hook deadline
            return answer(payload, **kwargs)

        transport.send = send
    ids = iter(f"rk{number:04d}" for number in range(10**4))
    engine = DecisionEngine(config, transport_factory=lambda _key: transport,
                            key_lookup=lambda: ApiKey("ts-test-" + "R" * 24), id_factory=lambda: next(ids),
                            monotonic=clock)
    items = [DecisionItem(ref, rank_legacy=index + 1) for index, ref in enumerate(RECALL_REFS)]
    state, bound = q.build_recall("how do we run the tests?", "memorymaster",
                                  [(ref, f"note {ref}") for ref in RECALL_REFS])
    ledger = DecisionLedger(db)
    logged = []
    for _ in range(n):
        made = engine.decide("recall", state=state, questions=bound, items=items, legacy_action=RECALL_REFS[:2],
                             choose=choose, context=DecisionContext(session_key="s", legacy_exposed=RECALL_REFS[:2]))
        assert made.mode == "live" and made.fallback_reason in ((None, "late") if late_every else (None,)), made
        values = transport.calls[-1]
        for ref in made.action:
            if values[ref][0] >= 0.7:
                _used(ledger, made.decision_id, ref, datetime.now(timezone.utc))
        logged.append((made, values))
    engine.wait_for_late_answers()
    return db, logged


def _topk_truth(logged, *, threshold, k_min, k_max):
    """True value of "order by relevance, k = usable >= threshold clamped to [k_min, k_max]"."""
    total = 0
    for _, values in logged:
        order = sorted(values, key=lambda ref: (-values[ref][0], RECALL_REFS.index(ref)))
        k = max(k_min, min(k_max, sum(1 for ref in order if values[ref][1] >= threshold)))
        total += sum(1 for ref in order[:k] if values[ref][0] >= 0.7)
    return total / len(logged)


def test_ope_topk_recovers_the_true_value_of_engine_logged_adaptive_k_rankings(tmp_path):
    """The candidate policy is Jev's own order and k, so every decision supports it.

    Its true value is the mean number of used candidates in Jev's top k.  Weighting a
    top-k action by the probability of the whole logged top-5 ordering inflates the
    weights (verifier OPE-TOPK-PROPENSITY); the prefix-marginal propensity is at least
    1 - rate for the policy's own top k.
    """
    db, logged = _engine_logged_recall(tmp_path, n=300, seed=11)
    n = len(logged)
    truth = sum(sum(1 for ref in made.jev_action if values[ref][0] >= 0.7) for made, values in logged) / n
    assert truth == pytest.approx(_topk_truth(logged, threshold=0.5, k_min=2, k_max=5))
    assert sum(made.exploration_arm != "policy" for made, _ in logged) > 45  # the exploration arm is exercised
    result = ope.evaluate(ope.open_ledger(db), surface="recall", policy="topk", questions=["recall.relevant"],
                          include_question="recall.usable_evidence", threshold=0.5, k_min=2, k_max=5,
                          reward_kind="used_in_turn", clip=10.0, bootstrap=300, seed=2,
                          now=datetime.now(timezone.utc) + timedelta(minutes=1))
    assert result["n"] == n and result["n_matched"] > 0.6 * n
    assert result["support"] == {"supported": n, "unsupported": 0, "unverified": 0, "coverage": 1.0}
    assert not any("identified" in warning for warning in result["warnings"]), result["warnings"]
    # A matched action is Jev's own top k, whose prefix-marginal propensity is >= 1 - rate = 0.7:
    # every weight lies in [1, 1/0.7], so nothing is clipped and (Kantorovich) ESS >= 0.969 * n_matched.
    assert result["clipping"]["clipped"] == 0
    assert result["ess"] >= 0.96 * result["n_matched"]
    for name in ("ips", "snips", "dr"):
        assert result["estimates"][name]["identified"] is True
        low, high = result["estimates"][name]["ci95"]
        assert low <= truth <= high, (name, truth, result["estimates"][name])


@pytest.mark.parametrize(("threshold", "k_min", "k_max"), [(0.3, 2, 5), (0.5, 2, 2)],
                         ids=["include-threshold-0.3", "fixed-k-2"])
def test_ope_topk_candidate_with_another_k_is_not_identified_by_ips(tmp_path, threshold, k_min, k_max):
    """Verifier OPE-TOPK-NO-SUPPORT: the engine explores only the order of the top 5, never k.

    A candidate whose k differs from the k Jev logged (or whose top k leaves the
    explored top 5) has logging probability 0 on that decision.  IPS and SNIPS keep
    such a decision with weight 0 and are biased toward 0 with an interval that
    excludes the truth, while ESS and n_matched look healthy.  They must be reported
    as not identified, with the support coverage and a warning; DR is kept with a
    caveat because it values those decisions by the reward model alone.
    """
    db, logged = _engine_logged_recall(tmp_path, n=300, seed=11)
    truth = _topk_truth(logged, threshold=threshold, k_min=k_min, k_max=k_max)
    result = ope.evaluate(ope.open_ledger(db), surface="recall", policy="topk", questions=["recall.relevant"],
                          include_question="recall.usable_evidence", threshold=threshold, k_min=k_min,
                          k_max=k_max, reward_kind="used_in_turn", clip=10.0, bootstrap=300, seed=3,
                          now=datetime.now(timezone.utc) + timedelta(minutes=1))
    assert any("not identified" in warning for warning in result["warnings"]), result["warnings"]
    support = result["support"]
    assert support["unsupported"] > 0 and support["unverified"] == 0
    assert support["supported"] + support["unsupported"] == result["n"] == len(logged)
    assert support["coverage"] == pytest.approx(support["supported"] / result["n"])
    assert support["supported"] >= result["n_matched"]  # "supported but not taken" is not "unsupported"
    for name in ("ips", "snips"):
        assert result["estimates"][name] == {"value": None, "ci95": None, "identified": False}, name
    dr = result["estimates"]["dr"]
    assert dr["identified"] is False and "reward model" in dr["caveat"]
    low, high = dr["ci95"]
    assert low <= truth <= high, (truth, dr)
    text = ope.format_text(result)
    assert "IPS: not identified" in text and "SNIPS: not identified" in text
    assert f"support: {support['supported']} of {result['n']}" in text


def test_ope_late_rows_are_legacy_for_the_candidate_and_keep_ips_identified(tmp_path):
    """Verifier probe: 5 % of the answers arrive after the hook deadline, candidate = Jev's own policy.

    A late row took the legacy action (p = 1) and a live candidate policy would be
    late there too, so its target on that row is the legacy action, not the ranking
    its (late) answers suggest.  Otherwise every late row is "unsupported" and IPS /
    SNIPS are never identified at realistic late rates.
    """
    db, logged = _engine_logged_recall(tmp_path, n=300, seed=11, late_every=20)
    late = [made for made, _ in logged if made.fallback_reason == "late"]
    assert len(late) == 15 and all(made.action == RECALL_REFS[:2] for made in late)
    truth = sum(sum(1 for ref in made.action if values[ref][0] >= 0.7) if made.fallback_reason == "late" else
                sum(1 for ref in made.jev_action if values[ref][0] >= 0.7) for made, values in logged) / len(logged)
    result = ope.evaluate(ope.open_ledger(db), surface="recall", policy="topk", questions=["recall.relevant"],
                          include_question="recall.usable_evidence", threshold=0.5, k_min=2, k_max=5,
                          reward_kind="used_in_turn", clip=10.0, bootstrap=300, seed=2,
                          now=datetime.now(timezone.utc) + timedelta(minutes=1))
    assert result["n"] == 300
    assert result["support"] == {"supported": 300, "unsupported": 0, "unverified": 0, "coverage": 1.0}
    assert not any("identified" in warning for warning in result["warnings"]), result["warnings"]
    for name in ("ips", "snips", "dr"):
        estimate = result["estimates"][name]
        assert estimate["identified"] is True, (name, estimate)
        low, high = estimate["ci95"]
        assert low <= truth <= high, (name, truth, estimate)


def _gate_decision(ledger, did, ts, g, *, reason=None, mode="live", taken="admit", propensities=None, chosen=1.0,
                   attempt=1):
    ledger.write_decision(DecisionRecord(
        decision_id=did, ts=utc_iso(ts), surface="ingest", mode=mode, fallback_reason=reason,
        transport_outcome="ok" if reason is None else reason, attempt_count=attempt,
        legacy_action=json.dumps("admit"), jev_action=None if reason else json.dumps(taken),
        action_taken=json.dumps(taken), available_actions_json=json.dumps(["admit", "hold"]),
        action_propensities_json=json.dumps(propensities or [{"action": taken, "p": 1.0}]),
        chosen_propensity=chosen,
        exploration_arm="fallback" if reason and reason != "breaker_open" else ("shadow" if reason else "policy"),
    ), [ItemRecord(decision_id=did, item_ref=f"claim:{did}", item_kind="claim", question_id="ingest.evidence",
                   question_version=1, answer=str(g))])


@pytest.mark.parametrize("reason", ["late", "timeout", "choose_error", "choose_invalid", "ledger_unavailable",
                                    "http_529", "http_5xx", "budget_exhausted", "egress_blocked"])
def test_ope_threshold_candidate_takes_legacy_on_every_fallback_row(tmp_path, reason):
    """Fallback rows logged answers that point the other way; the live engine would still have fallen back."""
    ledger, _ = _ope_ledger(tmp_path, n=200)
    start = NOW - timedelta(days=9)
    for index in range(10):  # ~5 %: answers below the gate, so the candidate's policy would say "hold"
        _gate_decision(ledger, f"fb{index}", start + timedelta(minutes=index), 0.1, reason=reason)
    result = ope.evaluate(ope.open_ledger(ledger.path), surface="ingest", policy="threshold",
                          questions=["ingest.evidence"], threshold=0.5, above="admit", below="hold",
                          reward_kind="steward_confirmed", bootstrap=50, seed=0, now=NOW)
    assert result["n"] == 210
    assert result["support"]["unsupported"] == 0 and result["support"]["supported"] == 210
    assert result["estimates"]["ips"]["identified"] is True and result["estimates"]["ips"]["value"] is not None


@pytest.mark.parametrize(("reason", "mode"), [("breaker_open", "shadow"), (None, "shadow")])
def test_ope_breaker_open_and_shadow_rows_keep_the_candidates_own_target(tmp_path, reason, mode):
    """An open breaker or shadow mode is not a failure of the request: the candidate is judged on its answers."""
    ledger, _ = _ope_ledger(tmp_path, n=200)
    for index in range(10):
        _gate_decision(ledger, f"sh{index}", NOW - timedelta(days=9, minutes=-index), 0.1, reason=reason, mode=mode)
    result = ope.evaluate(ope.open_ledger(ledger.path), surface="ingest", policy="threshold",
                          questions=["ingest.evidence"], threshold=0.5, above="admit", below="hold",
                          reward_kind="steward_confirmed", bootstrap=50, seed=0, now=NOW)
    assert result["support"]["unsupported"] == 10
    assert result["estimates"]["ips"] == {"value": None, "ci95": None, "identified": False}


def test_ope_skip_rows_are_not_units(tmp_path):
    """``skip:`` rows never asked Jev: they are neither support nor evidence for any candidate."""
    ledger, _ = _ope_ledger(tmp_path, n=200)
    for index in range(12):
        _gate_decision(ledger, f"sk{index}", NOW - timedelta(days=9, minutes=-index), 0.1,
                       reason="skip:no_candidates", attempt=0)
    result = ope.evaluate(ope.open_ledger(ledger.path), surface="ingest", policy="threshold",
                          questions=["ingest.evidence"], threshold=0.5, above="admit", below="hold",
                          reward_kind="steward_confirmed", bootstrap=50, seed=0, now=NOW)
    assert result["n"] == 200 and result["excluded_skip_rows"] == 12
    assert result["support"]["unsupported"] == 0


def test_ope_text_output_always_reports_n_ess_clipping_and_interval(tmp_path, capsys):
    empty = DecisionLedger(tmp_path / "empty.db")
    Q["decision"](empty, "only", ts=NOW - timedelta(days=1), surface="recall")
    code = ope.main(["--db", str(empty.path), "--surface", "ingest", "--policy", "threshold", "--question",
                     "ingest.evidence", "--threshold", "0.5", "--above", "admit", "--below", "hold",
                     "--reward-kind", "steward_confirmed", "--now", NOW.isoformat()])
    out = capsys.readouterr().out
    assert code == 0
    for label in ("n=0", "effective sample size", "clipping", "bootstrap", "95%"):
        assert label in out, label


# -------------------------------------------------------------------- export ---

def test_export_script_writes_time_split_jsonl(tmp_path):
    ledger = DecisionLedger(tmp_path / "d.db")
    Q["seed"](ledger, NOW)
    target = tmp_path / "rows.jsonl"
    split = (NOW - timedelta(days=2) + timedelta(minutes=30)).isoformat()
    assert exp.main(["--db", str(ledger.path), "--output", str(target), "--split-at", split]) == 0
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert {row["split"] for row in rows} == {"train", "test"}
    assert all(row["export_version"] == "jev-export/1" for row in rows)
    assert exp.main(["--db", str(tmp_path / "absent.db"), "--output", str(tmp_path / "none.jsonl")]) == 0
    assert (tmp_path / "none.jsonl").read_text(encoding="utf-8") == ""
    assert not (tmp_path / "absent.db").exists()


def test_export_script_refuses_to_write_over_the_ledger(tmp_path, capsys):
    """Opening ``--output`` for writing before reading would truncate an append-only ledger passed as output."""
    ledger = DecisionLedger(tmp_path / "d.db")
    Q["seed"](ledger, NOW)
    before = ledger.path.read_bytes()
    for target in (ledger.path, Path(f"{ledger.path}-wal"), tmp_path / "." / "d.db"):
        with pytest.raises(SystemExit) as exit_info:
            exp.main(["--db", str(ledger.path), "--output", str(target)])
        assert exit_info.value.code == 2
        assert "ledger" in capsys.readouterr().err
    assert ledger.path.read_bytes() == before
