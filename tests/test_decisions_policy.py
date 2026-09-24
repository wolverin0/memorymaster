"""Policy: deterministic exploration with logged propensities, breaker, USD/RPM budget."""
from __future__ import annotations

import itertools
import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from memorymaster.decisions import policy as pl
from memorymaster.decisions.config import DecisionConfig
from memorymaster.decisions.ledger import DecisionLedger, DecisionRecord, utc_iso

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def test_exploration_u_is_deterministic_and_uniform():
    assert pl.exploration_u("abc", "recall") == pl.exploration_u("abc", "recall")
    assert pl.exploration_u("abc", "recall") != pl.exploration_u("abc", "ingest")
    values = [pl.exploration_u(f"d{i}", "recall") for i in range(4000)]
    assert all(0.0 <= v < 1.0 for v in values)
    assert abs(sum(values) / len(values) - 0.5) < 0.03
    assert abs(sum(v < 0.1 for v in values) / len(values) - 0.1) < 0.02
    assert pl.randomization_id("abc", "recall") == pl.randomization_id("abc", "recall")


def test_binary_exploration_propensities_sum_to_one_and_match_choice():
    explored = 0
    for i in range(2000):
        result = pl.explore_binary(f"d{i}", "ingest", policy_action="hold", safe_alternative="admit",
                                   rate=0.05, arm_name="explore_admit")
        probs = dict(zip(map(json.dumps, result.available_actions), result.propensities))
        assert math.isclose(sum(result.propensities), 1.0)
        assert probs[json.dumps(result.action)] == pytest.approx(result.chosen_propensity)
        if result.arm == "explore_admit":
            explored += 1
            assert result.action == "admit" and result.chosen_propensity == pytest.approx(0.05)
        else:
            assert result.arm == "policy" and result.action == "hold"
            assert result.chosen_propensity == pytest.approx(0.95)
    assert 0.035 < explored / 2000 < 0.065


def test_binary_exploration_is_deterministic_per_decision():
    first = pl.explore_binary("fixed", "ingest", policy_action="hold", safe_alternative="admit", rate=0.5,
                              arm_name="explore_admit")
    for _ in range(5):
        again = pl.explore_binary("fixed", "ingest", policy_action="hold", safe_alternative="admit", rate=0.5,
                                  arm_name="explore_admit")
        assert again == first


@pytest.mark.parametrize("alt,rate", [(None, 0.05), ("hold", 0.05), ("admit", 0.0)])
def test_binary_without_alternative_is_deterministic_policy(alt, rate):
    result = pl.explore_binary("x", "ingest", policy_action="hold", safe_alternative=alt, rate=rate,
                               arm_name="explore_admit")
    assert result.arm == "policy" and result.action == "hold"
    assert result.available_actions == ["hold"] and result.propensities == [1.0]
    assert result.chosen_propensity == 1.0


def test_plackett_luce_probability_two_items():
    weights = {"a": 3.0, "b": 1.0}
    assert pl.plackett_luce_probability(["a", "b"], weights) == pytest.approx(0.75)
    assert pl.plackett_luce_probability(["b", "a"], weights) == pytest.approx(0.25)


def test_ranking_exploration_logs_full_distribution():
    order = ["c1", "c2", "c3", "c4", "c5", "c6", "c7"]
    scores = {"c1": 0.9, "c2": 0.8, "c3": 0.5, "c4": 0.4, "c5": 0.2, "c6": 0.1, "c7": 0.05}
    seen_arms = set()
    for i in range(300):
        result = pl.explore_ranking(f"r{i}", "recall", policy_order=order, scores=scores, rate=0.10)
        assert len(result.available_actions) == math.factorial(5)
        assert math.isclose(sum(result.propensities), 1.0, abs_tol=1e-9)
        index = result.available_actions.index(result.action[:5])
        assert result.propensities[index] == pytest.approx(result.chosen_propensity)
        assert result.action[5:] == ["c6", "c7"]
        assert sorted(result.action) == sorted(order)
        policy_index = result.available_actions.index(order[:5])
        assert result.propensities[policy_index] >= 0.9
        seen_arms.add(result.arm)
        if result.arm == "policy":
            assert result.action == order
    assert seen_arms == {"policy", "explore_order"}


def test_ranking_exploration_is_deterministic_and_small_lists_are_trivial():
    scores = {"a": 0.9, "b": 0.3, "c": 0.1}
    one = pl.explore_ranking("same", "recall", policy_order=["a", "b", "c"], scores=scores, rate=1.0)
    two = pl.explore_ranking("same", "recall", policy_order=["a", "b", "c"], scores=scores, rate=1.0)
    assert one == two
    assert len(one.available_actions) == 6
    single = pl.explore_ranking("x", "recall", policy_order=["a"], scores=scores, rate=1.0)
    assert single.arm == "policy" and single.propensities == [1.0]


def test_explored_rankings_follow_plackett_luce_shape():
    scores = {"a": 0.95, "b": 0.05}
    firsts = [pl.explore_ranking(f"s{i}", "recall", policy_order=["a", "b"], scores=scores, rate=1.0).action[0]
              for i in range(400)]
    share_a = firsts.count("a") / len(firsts)
    expected = pl.plackett_luce_probability(["a", "b"], pl.plackett_luce_weights(scores, ["a", "b"]))
    assert abs(share_a - expected) < 0.08


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_scores_are_refused_instead_of_logging_nan_propensities(bad):
    with pytest.raises(ValueError):
        pl.explore_ranking("x", "recall", policy_order=["a", "b", "c"], scores={"a": 0.9, "b": bad, "c": 0.1},
                           rate=0.10)


def test_large_scores_keep_a_finite_distribution():
    """Weights are shifted by the top score: exp(500 / 0.25) would overflow."""
    result = pl.explore_ranking("x", "recall", policy_order=["a", "b", "c"],
                                scores={"a": 500.0, "b": 499.5, "c": 499.0}, rate=0.10)
    assert all(math.isfinite(p) for p in result.propensities)
    assert math.isclose(sum(result.propensities), 1.0, abs_tol=1e-9)
    shifted = pl.plackett_luce_weights({"a": 0.5, "b": 0.0}, ["a", "b"])
    assert pl.plackett_luce_probability(["a", "b"], shifted) == pytest.approx(math.exp(2) / (math.exp(2) + 1))


def test_plackett_luce_survives_a_huge_finite_score_gap():
    """A gap of 500 is exp(-2000) in weight space: it must not underflow into 0/0."""
    order = ["a", "b", "c", "d"]
    scores = {"a": 500.0, "b": 0.0, "c": 0.0, "d": -1.0}
    logged = pl.explore_ranking("gap", "recall", policy_order=order, scores=scores, rate=0.10)
    assert all(math.isfinite(p) and p >= 0.0 for p in logged.propensities)
    assert math.isclose(sum(logged.propensities), 1.0, abs_tol=1e-9)
    log_weights = pl.plackett_luce_log_weights(scores, order)
    assert all(math.isfinite(v) for v in log_weights.values())
    log_p = pl.plackett_luce_log_probability(["b", "a", "c", "d"], log_weights)
    assert math.isfinite(log_p) and log_p < -1000  # b before a: astronomically unlikely, not NaN
    expected = 1 / (2 + math.exp(-4)) * 1 / (1 + math.exp(-4))
    assert math.exp(pl.plackett_luce_log_probability(order, log_weights)) == pytest.approx(expected)
    underflowed = pl.plackett_luce_weights(scores, order)  # b, c, d are exactly 0.0 here
    assert 0.0 <= pl.plackett_luce_probability(order, underflowed) <= 1.0
    assert pl.plackett_luce_probability(["b", "a", "c", "d"], underflowed) == 0.0
    for i in range(60):
        explored = pl.explore_ranking(f"gap{i}", "recall", policy_order=order, scores=scores, rate=1.0)
        assert explored.action[0] == "a"
        assert explored.chosen_propensity > 0.0
        assert math.isclose(sum(explored.propensities), 1.0, abs_tol=1e-9)


def _write(ledger: DecisionLedger, n: int, *, outcome: str, start: datetime, step_s: int = 1, attempt: int = 1,
           surface: str = "recall", prefix: str = "d") -> None:
    for i in range(n):
        ledger.write_decision(DecisionRecord(
            decision_id=f"{prefix}{i}-{outcome}-{start.timestamp()}", ts=utc_iso(start + timedelta(seconds=i * step_s)),
            surface=surface, mode="live", transport_outcome=outcome, attempt_count=attempt,
        ))


def test_breaker_opens_after_five_consecutive_failures_and_expires(tmp_path):
    ledger = DecisionLedger(tmp_path / "d.db")
    breaker = pl.Breaker(ledger)
    _write(ledger, 4, outcome="timeout", start=NOW - timedelta(minutes=5))
    assert breaker.is_open("recall", now=NOW) is False
    _write(ledger, 1, outcome="http_529", start=NOW - timedelta(minutes=1), prefix="e")
    assert breaker.is_open("recall", now=NOW) is True
    assert ledger.get_watermark("breaker:recall") is not None
    assert breaker.is_open("recall", now=NOW + timedelta(minutes=14)) is True
    assert breaker.is_open("ingest", now=NOW) is False
    _write(ledger, 3, outcome="ok", start=NOW + timedelta(minutes=10), prefix="ok")
    assert breaker.is_open("recall", now=NOW + timedelta(minutes=16)) is False


def test_breaker_consecutive_failures_only_count_within_the_last_ten_minutes(tmp_path):
    ledger = DecisionLedger(tmp_path / "d.db")
    breaker = pl.Breaker(ledger)
    _write(ledger, 5, outcome="timeout", start=NOW - timedelta(minutes=40))
    assert breaker.is_open("recall", now=NOW) is False  # five in a row, but all long ago
    _write(ledger, 4, outcome="timeout", start=NOW - timedelta(minutes=12), prefix="old")
    _write(ledger, 1, outcome="timeout", start=NOW - timedelta(minutes=2), prefix="new")
    assert breaker.is_open("recall", now=NOW) is False  # the older four do not chain with the recent one
    assert ledger.get_watermark("breaker:recall") is None
    _write(ledger, 4, outcome="http_529", start=NOW - timedelta(minutes=1), step_s=5, prefix="burst")
    assert breaker.is_open("recall", now=NOW) is True  # five failures inside the window


def test_breaker_opens_on_fallback_share_with_minimum_volume(tmp_path):
    ledger = DecisionLedger(tmp_path / "d.db")
    breaker = pl.Breaker(ledger)
    start = NOW - timedelta(minutes=5)
    _write(ledger, 6, outcome="ok", start=start, prefix="a")
    _write(ledger, 3, outcome="timeout", start=start + timedelta(seconds=20), prefix="b")
    assert breaker.is_open("recall", now=NOW) is False  # 9 decisions < minimum of 10
    _write(ledger, 1, outcome="ok", start=start + timedelta(seconds=40), prefix="c")
    assert breaker.is_open("recall", now=NOW) is False  # 3/10 = 30 % is not > 30 %
    _write(ledger, 1, outcome="http_429", start=start + timedelta(seconds=50), prefix="d")
    assert breaker.is_open("recall", now=NOW) is True  # 4/11 > 30 %


def test_breaker_ignores_decisions_that_never_sent_a_request(tmp_path):
    ledger = DecisionLedger(tmp_path / "d.db")
    _write(ledger, 12, outcome="egress_blocked", start=NOW - timedelta(minutes=2), attempt=0)
    assert pl.Breaker(ledger).is_open("recall", now=NOW) is False


def test_breaker_fails_closed_when_ledger_unreadable(tmp_path):
    blocker = tmp_path / "dir"
    blocker.mkdir()
    assert pl.Breaker(DecisionLedger(blocker)).is_open("recall", now=NOW) is True


def test_budget_daily_usd_cap(tmp_path):
    ledger = DecisionLedger(tmp_path / "d.db")
    config = DecisionConfig(daily_usd_cap=1.0)
    budget = pl.Budget(ledger, config)
    ledger.write_decision(DecisionRecord(decision_id="y", ts=utc_iso(NOW - timedelta(days=1)), cost_usd=50.0))
    ledger.write_decision(DecisionRecord(decision_id="a", ts=utc_iso(NOW - timedelta(hours=2)), cost_usd=0.9))
    assert budget.check(now=NOW, estimated_cost=0.05) is None
    assert budget.check(now=NOW, estimated_cost=0.2) == "budget_exhausted"


def test_budget_rpm_from_ledger_and_token_bucket(tmp_path):
    ledger = DecisionLedger(tmp_path / "d.db")
    config = DecisionConfig(rpm_cap=3)
    clock = [100.0]
    budget = pl.Budget(ledger, config, monotonic=lambda: clock[0])
    assert [budget.check(now=NOW, estimated_cost=0.0) for _ in range(4)] == [None, None, None, "budget_exhausted"]
    clock[0] += 20.0  # one token refills (3 per 60 s)
    assert budget.check(now=NOW, estimated_cost=0.0) is None
    fresh = pl.Budget(ledger, config)
    for i in range(3):
        ledger.write_decision(DecisionRecord(decision_id=f"r{i}", ts=utc_iso(NOW - timedelta(seconds=10)),
                                             attempt_count=1))
    assert fresh.check(now=NOW, estimated_cost=0.0) == "budget_exhausted"


def test_budget_fails_closed_when_ledger_unreadable(tmp_path):
    blocker = tmp_path / "dir"
    blocker.mkdir()
    assert pl.Budget(DecisionLedger(blocker), DecisionConfig()).check(now=NOW, estimated_cost=0.0) == "ledger_unavailable"


def test_thresholds_seeded_from_code_and_overridable_in_ledger(tmp_path):
    from memorymaster.decisions.questions import BoundQuestion, get

    ledger = DecisionLedger(tmp_path / "d.db")
    bound = [BoundQuestion(get("recall.usable_evidence"), "claim:1"), BoundQuestion(get("recall.relevant"), "claim:1")]
    thresholds = pl.resolve_thresholds(ledger, bound)
    assert thresholds["recall.usable_evidence@v1"] == {"include": 0.5, "k_max": 5, "k_min": 2}
    ledger.query("SELECT 1")
    import sqlite3

    conn = sqlite3.connect(tmp_path / "d.db")
    conn.execute("UPDATE question_versions SET threshold_json='{\"include\": 0.65}' "
                 "WHERE question_id='recall.usable_evidence'")
    conn.commit()
    conn.close()
    assert pl.resolve_thresholds(ledger, bound)["recall.usable_evidence@v1"] == {"include": 0.65}


def test_permutations_helper_matches_itertools():
    assert pl.orderings(["a", "b", "c"]) == [list(p) for p in itertools.permutations(["a", "b", "c"])]


def test_breaker_probe_is_claimed_once_per_interval(tmp_path):
    ledger = DecisionLedger(tmp_path / "d.db")
    breaker = pl.Breaker(ledger)
    ledger.write_decision(DecisionRecord(decision_id="old", surface="recall", attempt_count=1,
                                         transport_outcome="timeout", ts=utc_iso(NOW - timedelta(seconds=90))))
    assert breaker.claim_probe("recall", 60, now=NOW) is True
    assert breaker.claim_probe("recall", 60, now=NOW + timedelta(seconds=30)) is False  # claimed 30 s ago
    assert breaker.claim_probe("ingest", 60, now=NOW + timedelta(seconds=30)) is True  # per surface
    assert breaker.claim_probe("recall", 60, now=NOW + timedelta(seconds=61)) is True


def test_breaker_probe_waits_for_the_last_request_on_the_surface(tmp_path):
    ledger = DecisionLedger(tmp_path / "d.db")
    breaker = pl.Breaker(ledger)
    ledger.write_decision(DecisionRecord(decision_id="recent", surface="recall", attempt_count=2,
                                         transport_outcome="timeout", ts=utc_iso(NOW - timedelta(seconds=20))))
    ledger.write_decision(DecisionRecord(decision_id="unsent", surface="recall", attempt_count=0,
                                         transport_outcome="not_sent", ts=utc_iso(NOW - timedelta(seconds=1))))
    assert breaker.claim_probe("recall", 60, now=NOW) is False
    assert breaker.claim_probe("recall", 60, now=NOW + timedelta(seconds=41)) is True  # unsent rows do not count


def test_breaker_probe_fails_closed_on_an_unwritable_ledger(tmp_path):
    blocker = tmp_path / "decisions.db"
    blocker.mkdir()
    assert pl.Breaker(DecisionLedger(blocker)).claim_probe("recall", 60, now=NOW) is False


def test_breaker_fails_closed_when_only_the_watermark_read_fails(tmp_path, monkeypatch):
    """A failed read of the open-until watermark is not an absent watermark (verifier B1).

    Under concurrent hook writers a bounded read can meet 'database is locked'; the
    breaker must then report open (no live action, no spend), not fall through to
    the request history, which says closed.
    """
    ledger = DecisionLedger(tmp_path / "d.db")
    ledger.set_watermark("breaker:recall", utc_iso(NOW + timedelta(minutes=15)))
    real_read = ledger._read

    def locked_watermarks(sql, params=(), **kwargs):
        return None if "FROM watermarks" in sql else real_read(sql, params, **kwargs)

    monkeypatch.setattr(ledger, "_read", locked_watermarks)
    breaker = pl.Breaker(ledger)
    assert breaker.is_open("recall", now=NOW) is True
    assert breaker.state("recall", now=NOW) == "unknown"
    monkeypatch.undo()
    assert breaker.state("recall", now=NOW) == "open"
    assert pl.Breaker(DecisionLedger(tmp_path / "fresh.db")).state("recall", now=NOW) == "closed"


def test_budget_and_threshold_read_failures_fail_closed_and_are_counted(tmp_path):
    from memorymaster.decisions import ledger as ld
    from memorymaster.decisions import questions as q

    blocker = tmp_path / "dir"
    blocker.mkdir()
    broken = DecisionLedger(blocker)
    before = ld.ledger_write_failures()
    assert pl.Budget(broken, DecisionConfig()).check(now=NOW, estimated_cost=0.0) == "ledger_unavailable"
    assert ld.ledger_write_failures() > before
    _, bound = q.build_recall("how do we run tests?", "memorymaster", [("claim:1", "Run pytest")])
    counted = ld.ledger_write_failures()
    assert pl.resolve_thresholds(broken, bound, register=False) is None  # not silently the code defaults
    assert ld.ledger_write_failures() > counted
