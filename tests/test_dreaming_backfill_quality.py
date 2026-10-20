from memorymaster.dreaming.backfill_quality import evaluate_cohort_quality, receipt_is_current


class Judge:
    provider, model = "fixture", "haiku"

    def __init__(self, *, junk=(), leak=(), incomplete=False):
        self.junk, self.leak, self.incomplete = junk, leak, incomplete

    def judge(self, item, *, audit):
        if self.incomplete:
            return {}
        if audit == "missed_signal":
            return {"missed_useful": True, "secret": False, "cross_scope": False, "self_observation": False}
        key = item["candidate_id"]
        return {"junk": key in self.junk, "contradiction": False, "secret": key in self.leak,
                "cross_scope": False, "self_observation": False}


def test_exact_sample_denominators_and_junk_exclusion():
    candidates = [{"candidate_id": str(i)} for i in range(100)]
    discarded = [{"selection_id": str(i)} for i in range(60)]
    judge = Judge(junk={str(i) for i in range(5)})
    receipt = evaluate_cohort_quality(candidates, discarded, haiku=judge, sonnet=judge, identity={"source": "frozen"}, seed="fixed")
    assert receipt["passed"] and receipt["junk"] == 5 and receipt["judged"] == 100
    assert len(receipt["audit_ids"]) == 40 and receipt["spot_checked"] == 20
    assert len(receipt["known_junk_ids"]) == 5
    assert receipt_is_current(receipt, candidates, {"source": "frozen"})
    assert not receipt_is_current(receipt, candidates, {"source": "changed"})
    assert not receipt_is_current(receipt, candidates[:-1], {"source": "frozen"})


def test_small_population_observed_fraction_and_leak_latch():
    candidates = [{"candidate_id": "bad"}] + [{"candidate_id": str(i)} for i in range(19)]
    judge = Judge(junk={"bad"})
    receipt = evaluate_cohort_quality(candidates, [], haiku=judge, sonnet=judge, identity={}, seed="x")
    assert receipt["passed"] and receipt["judged"] == 20
    leak = Judge(leak={"bad"})
    receipt = evaluate_cohort_quality(candidates, [], haiku=leak, sonnet=leak, identity={}, seed="x")
    assert not receipt["passed"] and receipt["state"] == "quality_paused" and receipt["pause_scope"] == "campaign"


def test_incomplete_and_disagreement_fail_closed():
    candidates = [{"candidate_id": str(i)} for i in range(20)]
    receipt = evaluate_cohort_quality(candidates, [], haiku=Judge(), sonnet=Judge(incomplete=True), identity={}, seed="x")
    assert not receipt["passed"] and not receipt["complete"]
    receipt = evaluate_cohort_quality(candidates, [], haiku=Judge(), sonnet=Judge(junk={"1", "2", "3"}), identity={}, seed="x")
    assert not receipt["passed"] and receipt["agreements"] == 17


def test_sampling_reproducible_and_receipt_tamper_evident():
    candidates = [{"candidate_id": str(i)} for i in range(200)]
    kwargs = dict(haiku=Judge(), sonnet=Judge(), identity={}, seed="fixed")
    first = evaluate_cohort_quality(candidates, [], **kwargs)
    second = evaluate_cohort_quality(list(reversed(candidates)), [], **kwargs)
    assert first == second
    first["known_junk_ids"] = ["altered"]
    assert not receipt_is_current(first, candidates, {})


def test_six_percent_junk_latches_source_cohort_and_two_disagreements_pass():
    candidates = [{"candidate_id": str(i)} for i in range(100)]
    receipt = evaluate_cohort_quality(candidates, [], haiku=Judge(junk={str(i) for i in range(6)}),
                                      sonnet=Judge(), identity={}, seed="fixed")
    assert receipt["state"] == "quality_paused" and receipt["pause_scope"] == "cohort"
    candidates = candidates[:20]
    receipt = evaluate_cohort_quality(candidates, [], haiku=Judge(), sonnet=Judge(junk={"1", "2"}), identity={}, seed="fixed")
    assert receipt["passed"] and receipt["agreements"] == 18
    assert receipt["known_junk_ids"] == ["1", "2"]


def test_contradiction_audit_excludes_known_contradictory_claims():
    class Contradictions(Judge):
        def judge(self, item, *, audit):
            verdict = super().judge(item, audit=audit)
            verdict["contradiction"] = item["candidate_id"] == "1"
            return verdict

    candidates = [{"candidate_id": str(i)} for i in range(20)]
    receipt = evaluate_cohort_quality(candidates, [], haiku=Judge(), sonnet=Contradictions(), identity={}, seed="fixed")
    assert receipt["passed"] and receipt["contradictions"] == 1
    assert receipt["known_junk_ids"] == ["1"]


def test_missed_signal_audit_is_deterministic_and_leaks_pause_campaign():
    class AuditLeak(Judge):
        def judge(self, item, *, audit):
            result = super().judge(item, audit=audit)
            if audit == "missed_signal":
                result["cross_scope"] = True
            return result

    discarded = [{"selection_id": str(i)} for i in range(60)]
    kwargs = dict(haiku=AuditLeak(), sonnet=Judge(), identity={}, seed="fixed")
    first = evaluate_cohort_quality([], discarded, **kwargs)
    assert first == evaluate_cohort_quality([], list(reversed(discarded)), **kwargs)
    assert not first["passed"] and first["pause_scope"] == "campaign"


def test_provider_judge_uses_existing_bounded_client_and_checks_observed_model():
    import json
    from types import SimpleNamespace

    import pytest

    from memorymaster.dreaming.backfill_quality import ProviderCohortJudge

    calls = []
    model = "anthropic/claude-haiku-4-5"

    def complete(prompt, *, max_output_tokens):
        calls.append(max_output_tokens)
        return SimpleNamespace(text=json.dumps({"junk": False}), input_tokens=100,
                               output_tokens=10, observed_model=model)

    judge = ProviderCohortJudge(SimpleNamespace(complete_bounded=complete), provider="anthropic", model=model)
    assert judge.judge({"candidate_id": "one"}, audit="precision") == {"junk": False}
    assert calls == [300] and judge.last_receipt["observed_model"] == model
    model = "anthropic/wrong-model"
    with pytest.raises(Exception, match="model identity"):
        judge.judge({"candidate_id": "one"}, audit="precision")
    with pytest.raises(ValueError, match="revalidation"):
        judge.judge({"candidate_id": "one"}, audit="precision")
    assert len(calls) == 2


def test_changed_discard_population_or_model_identity_invalidates_release():
    candidates, discarded = [{"candidate_id": "one"}], [{"selection_id": "old"}]
    identity = {"models": "frozen-models"}
    receipt = evaluate_cohort_quality(candidates, discarded, haiku=Judge(), sonnet=Judge(), identity=identity, seed="fixed")
    assert receipt_is_current(receipt, candidates, identity, discarded=discarded)
    assert not receipt_is_current(receipt, candidates, identity, discarded=[{"selection_id": "new"}])
    assert not receipt_is_current(receipt, candidates, {"models": "new-models"})
