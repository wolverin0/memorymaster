"""Surface S3 INGEST: Jev triage of Dreaming candidates between extraction and consolidation.

Live: a candidate that passes all eight source-review checks proceeds to
consolidation; otherwise it is ``held`` (annotated in ``extraction_json``,
skipped by consolidation, never deleted) and the capture still completes. 5 %
of held candidates are admitted anyway (arm ``explore_admit``, propensity
logged). Every failure falls back to the legacy path (no triage) and is logged.
``held.release`` un-holds candidates and records ``held_released`` outcomes.
No test reaches TypeSafe: the engine gets a fake transport and a fake key.
"""
from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest

from memorymaster.decisions import policy
from memorymaster.decisions.config import DecisionConfig
from memorymaster.decisions.credentials import ApiKey
from memorymaster.decisions.engine import DecisionEngine
from memorymaster.decisions.ledger import DecisionLedger
from memorymaster.decisions.transport import ParsedAnswer, TransportResult
from memorymaster.dreaming import held
from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.worker import DreamConfig, DreamWorker

from test_dreaming_worker import (  # noqa: E402  (shared fixtures of the worker suite)
    NOW,
    _capture,
    _Consolidator,
    _Extractor,
    _NoCall,
    _service,
)

KEY = ApiKey("ts-test-" + "F" * 24)  # synthetic
PROJECT, PERSONAL = "project-c", "personal-c"


class FakeJev:
    """Answers every ingest noul with ``values(check, candidate_id)``; records requests."""

    def __init__(self, values=lambda check, candidate: 0.9, outcome: str = "ok", delay: float = 0.0):
        self.values = values
        self.outcome = outcome
        self.delay = delay
        self.calls: list[dict] = []

    def send(self, payload, *, expected, timeout_s, max_retries=0):
        self.calls.append({"payload": payload, "timeout_s": timeout_s, "max_retries": max_retries})
        if self.delay:
            time.sleep(self.delay)
        if self.outcome != "ok":
            return TransportResult(503, None, 12, 1, self.outcome)
        answers = {}
        for wire_id in expected:
            question, ref = wire_id.split("::", 1)
            value = self.values(question.removeprefix("ingest."), ref.rsplit(":", 1)[-1])
            answers[wire_id] = ParsedAnswer("noul", value, {"noul": value})
        return TransportResult(200, {"model": "jev-1.13.0"}, 440, 1, "ok", model_served="jev-1.13.0",
                               tokens_in=600, tokens_out=8, cost_usd=600 * 0.042e-6, answers=answers)

    def close(self):
        pass


def _ids(explore: bool):
    """Decision ids whose deterministic exploration draw is below/above the 5 % ingest rate."""
    wanted, n = [], 0
    while len(wanted) < 50:
        candidate = f"dec-{n}"
        if (policy.exploration_u(candidate, "ingest") < 0.05) == explore:
            wanted.append(candidate)
        n += 1
    return iter(wanted)


def _engine(tmp_path, jev=None, *, mode="live", key=KEY, explore=False, env=None):
    environ = {"MEMORYMASTER_JEV_MODE": mode, "MEMORYMASTER_DECISIONS_DB": str(tmp_path / "decisions.db")}
    environ.update(env or {})
    ids = _ids(explore)
    jev = jev or FakeJev()
    engine = DecisionEngine(DecisionConfig.from_env(environ), transport_factory=lambda _key: jev,
                            key_lookup=lambda: key, id_factory=lambda: next(ids))
    return engine, jev


class _Recording(_Consolidator):
    def __init__(self) -> None:
        super().__init__()
        self.seen: list[list[str]] = []

    def consolidate(self, candidates, current_claims, *, scope):
        self.seen.append([candidate.candidate_id for candidate in candidates])
        return super().consolidate(candidates, current_claims, scope=scope)


def _worker(tmp_path, ledger, engine, *, extractor=None, consolidator=None, config=None):
    return DreamWorker(ledger, _service(tmp_path), extractor or _Extractor(), consolidator or _Recording(),
                       config=config or DreamConfig(), now=lambda: NOW, decision_engine=engine)


def _notes(ledger: DreamLedger, capture_id: int) -> dict[str, dict | None]:
    return {payload["candidate_id"]: payload.get(held.ANNOTATION)
            for payload in ledger.get_capture(capture_id)["extraction"]}


def _decisions(tmp_path) -> list[dict]:
    return DecisionLedger(tmp_path / "decisions.db").query("SELECT * FROM decisions ORDER BY ts")


def _fails_privacy_for(candidate_id: str):
    return lambda check, candidate: 0.1 if (check == "privacy" and candidate == candidate_id) else 0.9


def test_live_candidates_that_pass_every_check_proceed_to_consolidation(tmp_path):
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    engine, jev = _engine(tmp_path)
    consolidator = _Recording()

    summary = _worker(tmp_path, ledger, engine, consolidator=consolidator).run(apply_candidates=False)

    assert sorted(sum(consolidator.seen, [])) == sorted([PROJECT, PERSONAL])
    assert ledger.get_capture(capture_id)["state"] == "consolidated"
    assert (summary["ingest_triaged"], summary["ingest_held"]) == (2, 0)
    assert {note["status"] for note in _notes(ledger, capture_id).values()} == {"admitted"}
    # One request per candidate, with the batch deadline and batch retries.
    assert len(jev.calls) == 2
    assert {(call["timeout_s"], call["max_retries"]) for call in jev.calls} == {(8.0, 3)}
    payload = jev.calls[0]["payload"]
    assert set(payload["state"]) == {"candidate", "evidence", "scope", "excerpt"}
    assert sorted(q.split("::")[0] for q in payload["questions"]) == sorted(
        f"ingest.{check}" for check in ("evidence", "chronology", "modality", "scope", "specificity",
                                        "privacy", "usefulness", "novelty"))
    rows = _decisions(tmp_path)
    assert [(row["surface"], row["mode"], json.loads(row["action_taken"])) for row in rows] == [
        ("ingest", "live", "admit")] * 2
    assert {row["fallback_reason"] for row in rows} == {None}


def test_live_failed_check_holds_the_candidate_and_the_capture_still_completes(tmp_path):
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    engine, _jev = _engine(tmp_path, FakeJev(_fails_privacy_for(PROJECT)))
    consolidator = _Recording()

    summary = _worker(tmp_path, ledger, engine, consolidator=consolidator).run(apply_candidates=True)

    assert consolidator.seen == [[PERSONAL]]  # held candidates never reach consolidation
    assert summary["ingest_held"] == 1
    capture = ledger.get_capture(capture_id)
    assert capture["state"] == "applied"  # the capture completes around the held candidate
    assert [d["candidate_id"] for d in capture["decisions"]] == [PERSONAL]
    assert capture["held_count"] == 1
    note = _notes(ledger, capture_id)[PROJECT]
    assert note["status"] == "held" and note["item_ref"] == held.item_ref(capture_id, PROJECT)
    row = next(r for r in _decisions(tmp_path) if r["decision_id"] == note["decision_id"])
    assert (json.loads(row["action_taken"]), json.loads(row["jev_action"]), row["exploration_arm"]) == (
        "hold", "hold", "policy")
    assert row["chosen_propensity"] == pytest.approx(0.95)
    assert [a["action"] for a in json.loads(row["action_propensities_json"])] == ["hold", "admit"]
    # Held means kept (ruling R7): retention does not prune a capture that still
    # holds a candidate, for up to MEMORYMASTER_DREAM_HELD_RETAIN_DAYS after the hold.
    ledger.prune(retain_days=0, max_bytes=0, now=NOW + timedelta(days=29))
    assert ledger.get_capture(capture_id)["held_count"] == 1


def test_exploration_admits_a_held_candidate_with_its_propensity(tmp_path):
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    engine, _jev = _engine(tmp_path, FakeJev(_fails_privacy_for(PROJECT)), explore=True)
    consolidator = _Recording()

    summary = _worker(tmp_path, ledger, engine, consolidator=consolidator).run(apply_candidates=False)

    assert sorted(sum(consolidator.seen, [])) == sorted([PROJECT, PERSONAL])
    assert (summary["ingest_held"], summary["ingest_explored"]) == (0, 1)
    note = _notes(ledger, capture_id)[PROJECT]
    assert (note["status"], note["arm"]) == ("admitted", "explore_admit")
    row = next(r for r in _decisions(tmp_path) if r["decision_id"] == note["decision_id"])
    assert (json.loads(row["action_taken"]), json.loads(row["jev_action"])) == ("admit", "hold")
    assert row["exploration_arm"] == "explore_admit"
    assert row["chosen_propensity"] == pytest.approx(0.05)


@pytest.mark.parametrize(("setup", "reason"), [
    pytest.param(dict(jev=FakeJev(outcome="http_5xx")), "http_5xx", id="http-5xx"),
    pytest.param(dict(key=None), "missing_key", id="missing-key"),
    pytest.param(dict(jev=FakeJev(_fails_privacy_for(PROJECT), delay=0.4),
                      env={"MEMORYMASTER_JEV_BATCH_DEADLINE_MS": "50"}), "timeout", id="late-answer"),
    pytest.param(dict(jev=FakeJev(_fails_privacy_for(PROJECT)),
                      env={"MEMORYMASTER_JEV_DAILY_USD_CAP": "0.0000001"}), "budget_exhausted", id="budget"),
])
def test_every_failure_falls_back_to_the_legacy_path_and_is_logged(tmp_path, setup, reason):
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    engine, _jev = _engine(tmp_path, **setup)
    consolidator = _Recording()

    _worker(tmp_path, ledger, engine, consolidator=consolidator).run(apply_candidates=False)
    engine.wait_for_late_answers(5.0)

    assert sorted(sum(consolidator.seen, [])) == sorted([PROJECT, PERSONAL])  # legacy: no triage
    notes = _notes(ledger, capture_id)
    assert {note["status"] for note in notes.values()} == {"admitted"}
    assert {note["fallback_reason"] for note in notes.values()} == {reason}
    rows = _decisions(tmp_path)
    assert len(rows) == 2 and {row["fallback_reason"] for row in rows} == {reason}
    assert {json.loads(row["action_taken"]) for row in rows} == {"admit"}


def test_shadow_logs_jev_but_takes_the_legacy_action(tmp_path):
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    engine, _jev = _engine(tmp_path, FakeJev(_fails_privacy_for(PROJECT)), mode="shadow")
    consolidator = _Recording()

    _worker(tmp_path, ledger, engine, consolidator=consolidator).run(apply_candidates=False)

    assert sorted(sum(consolidator.seen, [])) == sorted([PROJECT, PERSONAL])
    note = _notes(ledger, capture_id)[PROJECT]
    assert (note["status"], note["mode"], note["jev_action"]) == ("admitted", "shadow", "hold")


def test_off_mode_sends_nothing_writes_nothing_and_leaves_extraction_untouched(tmp_path):
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    engine, jev = _engine(tmp_path, mode="off")

    _worker(tmp_path, ledger, engine).run(apply_candidates=False)

    assert jev.calls == []
    assert not (tmp_path / "decisions.db").exists()
    assert _notes(ledger, capture_id) == {PROJECT: None, PERSONAL: None}


def test_each_candidate_is_triaged_once_across_runs(tmp_path):
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    engine, jev = _engine(tmp_path, FakeJev(_fails_privacy_for(PROJECT)))
    deferred = DreamConfig(max_consolidate_calls_daily=0)  # consolidation waits for budget

    for _ in range(3):
        _worker(tmp_path, ledger, engine, consolidator=_NoCall(), config=deferred).run(apply_candidates=False)

    assert len(jev.calls) == 2
    assert _notes(ledger, capture_id)[PROJECT]["status"] == "held"
    assert len(_decisions(tmp_path)) == 2


def test_release_unholds_records_the_outcome_and_the_next_run_consolidates_it(tmp_path):
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    engine, jev = _engine(tmp_path, FakeJev(_fails_privacy_for(PROJECT)))
    service = _service(tmp_path)
    first = _Recording()
    DreamWorker(ledger, service, _Extractor(), first, config=DreamConfig(), now=lambda: NOW,
                decision_engine=engine).run(apply_candidates=True)
    hold_decision = _notes(ledger, capture_id)[PROJECT]["decision_id"]
    assert ledger.get_capture(capture_id)["state"] == "applied"

    decisions = DecisionLedger(tmp_path / "decisions.db")
    assert held.release(ledger, capture_id=capture_id, actor="operator", decisions=decisions) == 1
    assert held.release(ledger, capture_id=capture_id, actor="operator", decisions=decisions) == 0

    capture = ledger.get_capture(capture_id)
    assert (capture["state"], capture["held_count"]) == ("extracted", 0)
    note = _notes(ledger, capture_id)[PROJECT]
    assert (note["status"], note["released_by"], note["decision_id"]) == ("released", "operator", hold_decision)
    outcomes = decisions.query("SELECT decision_id, item_ref, kind, label_source FROM outcomes "
                               "WHERE kind = 'held_released'")
    assert outcomes == [{"decision_id": hold_decision, "item_ref": held.item_ref(capture_id, PROJECT),
                         "kind": "held_released", "label_source": "operator"}]

    second = _Recording()
    DreamWorker(ledger, service, _NoCall(), second, config=DreamConfig(), now=lambda: NOW,
                decision_engine=engine).run(apply_candidates=True)

    assert second.seen == [[PROJECT]]  # only the released candidate, never re-triaged
    assert len(jev.calls) == 2
    capture = ledger.get_capture(capture_id)
    assert capture["state"] == "applied"
    assert sorted(d["candidate_id"] for d in capture["decisions"]) == sorted([PROJECT, PERSONAL])
    texts = {claim.text for claim in service.list_claims(status="candidate", limit=20,
                                                         scope_allowlist=["project:test", "personal"])}
    assert "The blue interface is selected for this project." in texts


def test_release_filters_by_candidate_and_never_creates_ledgers(tmp_path):
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    engine, _jev = _engine(tmp_path, FakeJev(lambda check, candidate: 0.1 if check == "novelty" else 0.9))
    _worker(tmp_path, ledger, engine, consolidator=_NoCall(),
            config=DreamConfig(max_consolidate_calls_daily=0)).run(apply_candidates=False)
    assert ledger.get_capture(capture_id)["held_count"] == 2

    missing_decisions = tmp_path / "absent" / "decisions.db"
    assert held.release(str(ledger.db_path), candidate_id=PERSONAL, decisions=missing_decisions) == 1
    assert not missing_decisions.exists()
    notes = _notes(ledger, capture_id)
    assert (notes[PERSONAL]["status"], notes[PROJECT]["status"]) == ("released", "held")
    assert ledger.get_capture(capture_id)["held_count"] == 1

    absent = tmp_path / "no-ledger" / "capture.db"
    assert held.release(absent) == 0
    assert not absent.exists()


def _later_runs(tmp_path, ledger, engine, service, runs=3) -> _Recording:
    later = _Recording()
    for _ in range(runs):
        DreamWorker(ledger, service, _NoCall(), later, config=DreamConfig(), now=lambda: NOW,
                    decision_engine=engine).run(apply_candidates=True)
    return later


def _assert_released_candidate_reached_memory(ledger, capture_id, service, later):
    capture = ledger.get_capture(capture_id)
    assert later.seen == [[PROJECT]]  # consolidated once, on the next run, never re-triaged
    assert (capture["state"], capture["held_count"]) == ("applied", 0)
    assert sorted(d["candidate_id"] for d in capture["decisions"]) == sorted([PROJECT, PERSONAL])
    texts = {claim.text for claim in service.list_claims(status="candidate", limit=20,
                                                         scope_allowlist=["project:test", "personal"])}
    assert "The blue interface is selected for this project." in texts


def test_release_while_the_worker_consolidates_the_capture_is_not_lost(tmp_path):
    """Review B1(a): release() does not wait for a running worker.

    The capture is ``extracted`` while consolidation runs, so the release only
    changes the annotation; the worker then saves decisions without the
    released candidate. The capture must stay resumable, not end ``applied``.
    """
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    engine, jev = _engine(tmp_path, FakeJev(_fails_privacy_for(PROJECT)))
    decisions = DecisionLedger(tmp_path / "decisions.db")
    service = _service(tmp_path)
    released: list[int] = []

    class ReleasingMidRun(_Recording):
        def consolidate(self, candidates, current_claims, *, scope):
            if not released:  # the operator runs jev-release-held while Dreaming runs
                released.append(held.release(ledger, capture_id=capture_id, decisions=decisions))
            return super().consolidate(candidates, current_claims, scope=scope)

    DreamWorker(ledger, service, _Extractor(), ReleasingMidRun(), config=DreamConfig(), now=lambda: NOW,
                decision_engine=engine).run(apply_candidates=True)

    assert released == [1]
    assert ledger.get_capture(capture_id)["state"] == "extracted"  # not done: PROJECT is undecided
    _assert_released_candidate_reached_memory(ledger, capture_id, service,
                                              _later_runs(tmp_path, ledger, engine, service))
    assert len(jev.calls) == 2
    assert [row["kind"] for row in decisions.query(
        "SELECT kind FROM outcomes WHERE kind LIKE 'held%'")] == ["held_released"]


def test_release_while_the_worker_applies_the_capture_is_not_lost(tmp_path):
    """Review B1(b): the release reopens the capture, then the worker's mark_applied must not close it."""
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    engine, _jev = _engine(tmp_path, FakeJev(_fails_privacy_for(PROJECT)))
    decisions = DecisionLedger(tmp_path / "decisions.db")
    service = _service(tmp_path)
    DreamWorker(ledger, service, _Extractor(), _Recording(), config=DreamConfig(), now=lambda: NOW,
                decision_engine=engine).run(apply_candidates=False)
    assert ledger.get_capture(capture_id)["state"] == "consolidated"

    worker = DreamWorker(ledger, service, _NoCall(), _NoCall(), config=DreamConfig(), now=lambda: NOW,
                         decision_engine=engine)
    apply_capture = worker._apply_capture
    released: list[int] = []

    def releasing_mid_apply(run_id, row, summary):
        released.append(held.release(ledger, capture_id=capture_id, decisions=decisions))
        return apply_capture(run_id, row, summary)

    worker._apply_capture = releasing_mid_apply
    summary = worker.run(apply_candidates=True)

    assert released == [1] and summary["applied"] == 1  # PERSONAL was applied in that run
    assert ledger.get_capture(capture_id)["state"] == "extracted"
    _assert_released_candidate_reached_memory(ledger, capture_id, service,
                                              _later_runs(tmp_path, ledger, engine, service))


def test_release_skips_a_quarantined_capture_which_retention_never_exempts(tmp_path):
    """Review B2: a quarantined capture is never consolidated again, so a release
    could not be honoured. Ruling R7: nor is it exempt from retention."""
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    engine, _jev = _engine(tmp_path, FakeJev(_fails_privacy_for(PROJECT)))
    service = _service(tmp_path)

    class Invalid(_Consolidator):
        def consolidate(self, candidates, current_claims, *, scope):
            raise ValueError("decision references an unknown candidate")

    for _ in range(3):
        DreamWorker(ledger, service, _Extractor(), Invalid(), config=DreamConfig(max_semantic_attempts=2),
                    now=lambda: NOW, decision_engine=engine).run(apply_candidates=False)
    assert (ledger.get_capture(capture_id)["state"], ledger.get_capture(capture_id)["held_count"]) == (
        "quarantined", 1)
    hold_note = _notes(ledger, capture_id)[PROJECT]

    decisions = DecisionLedger(tmp_path / "decisions.db")
    assert held.release(ledger, capture_id=capture_id, decisions=decisions) == 0
    assert held.release(ledger, decisions=decisions) == 0  # unfiltered release skips it too

    capture = ledger.get_capture(capture_id)
    assert (capture["state"], capture["held_count"]) == ("quarantined", 1)
    assert _notes(ledger, capture_id)[PROJECT] == hold_note
    assert decisions.query("SELECT kind FROM outcomes") == []
    # dream-status tells the operator why nothing was released.
    assert DreamLedger.read_status(ledger.db_path)["held"] == {
        "captures": 1, "candidates": 1, "quarantined_captures": 1}
    # Ruling R7: a quarantined capture is never exempt from retention; its held
    # candidate leaves a held_expired outcome when the normal rules prune it.
    result = ledger.prune(retain_days=0, max_bytes=0, now=NOW + timedelta(days=1))
    assert result["deleted"] == 1
    with pytest.raises(KeyError):
        ledger.get_capture(capture_id)
    assert [(n["decision_id"], n["item_ref"], n["reason"]) for n in result["held_expired"]] == [
        (hold_note["decision_id"], hold_note["item_ref"], "quarantined")]


@pytest.mark.parametrize("broken", ["capture_excerpt", "build_ingest"])
def test_a_triage_error_outside_the_engine_falls_back_to_the_legacy_path(tmp_path, monkeypatch, broken):
    """Building the S3 state must not fail the Dreaming run: legacy path, logged like any fallback."""
    from memorymaster.decisions import questions

    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    engine, jev = _engine(tmp_path, FakeJev(_fails_privacy_for(PROJECT)))
    consolidator = _Recording()

    def bug(*_args, **_kwargs):
        raise RuntimeError(f"{broken} bug")

    monkeypatch.setattr(held if broken == "capture_excerpt" else questions, broken, bug)
    summary = _worker(tmp_path, ledger, engine, consolidator=consolidator).run(apply_candidates=False)

    assert summary["ok"] is True and "fatal" not in summary
    assert sorted(sum(consolidator.seen, [])) == sorted([PROJECT, PERSONAL])
    assert ledger.get_capture(capture_id)["state"] == "consolidated"
    assert jev.calls == []  # nothing was sent
    # Every fallback reaches the decisions ledger (contract): an invalid_request
    # row per candidate, joined to the candidate by its annotation and item_ref.
    notes = _notes(ledger, capture_id)
    assert {(note["status"], note["fallback_reason"]) for note in notes.values()} == {("admitted", "invalid_request")}
    rows = _decisions(tmp_path)
    assert [(row["surface"], row["fallback_reason"], json.loads(row["action_taken"])) for row in rows] == [
        ("ingest", "invalid_request", "admit")] * 2
    assert {note["decision_id"] for note in notes.values()} == {row["decision_id"] for row in rows}
    items = DecisionLedger(tmp_path / "decisions.db").query("SELECT item_ref FROM decision_items")
    assert sorted(row["item_ref"] for row in items) == sorted(
        held.item_ref(capture_id, candidate) for candidate in (PROJECT, PERSONAL))
    assert (summary["ingest_triaged"], summary["ingest_held"]) == (2, 0)


def test_capture_excerpt_is_bounded_and_centred_on_the_evidence():
    messages = [{"message_id": f"m{i}", "role": "user", "text": ("filler " * 60).strip()} for i in range(10)]
    messages[6]["text"] = "the evidence quote lives here and a later correction follows"
    candidate = {"evidence_message_id": "m6", "evidence_quote": "evidence quote lives here"}

    excerpt = held.capture_excerpt(messages, candidate, limit=400)

    assert len(excerpt) <= 400
    assert "evidence quote lives here" in excerpt
    assert not excerpt.startswith("iller") and not excerpt.endswith("fille")


# ------------------------------------------------ ruling R7: held retention ---

def _held_capture(tmp_path, ledger):
    """A capture that applied its admitted candidate and holds PROJECT."""
    capture_id = _capture(ledger)
    engine, _jev = _engine(tmp_path, FakeJev(_fails_privacy_for(PROJECT)))
    _worker(tmp_path, ledger, engine).run(apply_candidates=True)
    capture = ledger.get_capture(capture_id)
    assert (capture["state"], capture["held_count"]) == ("applied", 1)
    return capture_id, engine, _notes(ledger, capture_id)[PROJECT]


def _held_outcomes(tmp_path, kind="held_expired"):
    return DecisionLedger(tmp_path / "decisions.db").query(
        "SELECT decision_id, item_ref, kind, label_source, details_json FROM outcomes WHERE kind = ?", [kind])


def test_a_held_capture_is_exempt_from_retention_and_the_byte_cap_only_within_the_window(tmp_path):
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id, _engine_, note = _held_capture(tmp_path, ledger)

    within = ledger.prune(retain_days=0, max_bytes=0, now=NOW + timedelta(days=29), held_retain_days=30)
    assert within == {"deleted": 0, "held_expired": []}
    assert ledger.get_capture(capture_id)["held_count"] == 1

    after = ledger.prune(retain_days=0, max_bytes=0, now=NOW + timedelta(days=31), held_retain_days=30)
    assert after["deleted"] == 1
    with pytest.raises(KeyError):
        ledger.get_capture(capture_id)
    [expired] = after["held_expired"]
    assert (expired["decision_id"], expired["item_ref"], expired["reason"]) == (
        note["decision_id"], note["item_ref"], "held_retention_expired")


def test_an_expired_hold_is_pruned_by_the_byte_cap_too(tmp_path):
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id, _engine_, _note = _held_capture(tmp_path, ledger)

    # retain_days keeps it by age; only the byte cap applies once the exemption is over.
    kept = ledger.prune(retain_days=3650, max_bytes=0, now=NOW + timedelta(days=5), held_retain_days=30)
    assert kept["deleted"] == 0
    capped = ledger.prune(retain_days=3650, max_bytes=0, now=NOW + timedelta(days=31), held_retain_days=30)
    assert capped["deleted"] == 1 and len(capped["held_expired"]) == 1
    with pytest.raises(KeyError):
        ledger.get_capture(capture_id)


def test_the_worker_records_held_expired_in_the_decisions_ledger(tmp_path):
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id, engine, note = _held_capture(tmp_path, ledger)
    config = DreamConfig(retain_days=7, max_capture_bytes=0, held_retain_days=30)  # byte cap: wall-clock free

    DreamWorker(ledger, _service(tmp_path), _NoCall(), _NoCall(), config=config,
                now=lambda: NOW + timedelta(days=20), decision_engine=engine).run(apply_candidates=True)
    assert ledger.get_capture(capture_id)["held_count"] == 1 and _held_outcomes(tmp_path) == []

    DreamWorker(ledger, _service(tmp_path), _NoCall(), _NoCall(), config=config,
                now=lambda: NOW + timedelta(days=31), decision_engine=engine).run(apply_candidates=True)

    with pytest.raises(KeyError):
        ledger.get_capture(capture_id)
    [row] = _held_outcomes(tmp_path)
    assert (row["decision_id"], row["item_ref"], row["label_source"]) == (
        note["decision_id"], note["item_ref"], "surface")
    assert json.loads(row["details_json"])["reason"] == "held_retention_expired"


def test_held_retain_days_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("MEMORYMASTER_DREAM_HELD_RETAIN_DAYS", raising=False)
    assert DreamConfig.from_env().held_retain_days == 30
    monkeypatch.setenv("MEMORYMASTER_DREAM_HELD_RETAIN_DAYS", "12")
    assert DreamConfig.from_env().held_retain_days == 12


# ------------------------------------------------------ S3 rewards (item 8) ---

def _outcomes_of(tmp_path, kind):
    return DecisionLedger(tmp_path / "decisions.db").query(
        "SELECT decision_id, item_ref, kind, value, label_source, details_json FROM outcomes WHERE kind = ? "
        "ORDER BY item_ref", [kind])


def _applications(ledger, capture_id):
    with ledger._connect() as conn:
        return [{"candidate_id": row[0], "created_claim_id": row[1]} for row in conn.execute(
            "SELECT candidate_id, created_claim_id FROM dream_applications WHERE capture_id=?", (capture_id,))]


class _IgnoresPersonal(_Consolidator):
    def consolidate(self, candidates, current_claims, *, scope):
        result = super().consolidate(candidates, current_claims, scope=scope)
        from memorymaster.dreaming.models import ConsolidationResult, DreamDecision

        decisions = tuple(
            DreamDecision(d.candidate_id, "ignore", "synthetic rejection", 0.9) if d.candidate_id == PERSONAL else d
            for d in result.decisions)
        return ConsolidationResult(decisions, result.usage)


def test_every_triaged_candidate_that_reaches_consolidation_gets_the_verdict(tmp_path):
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    engine, _jev = _engine(tmp_path)

    _worker(tmp_path, ledger, engine, consolidator=_IgnoresPersonal()).run(apply_candidates=False)

    notes = _notes(ledger, capture_id)
    applied = _outcomes_of(tmp_path, "consolidation_applied")
    rejected = _outcomes_of(tmp_path, "consolidation_rejected")
    assert [(r["decision_id"], r["item_ref"]) for r in applied] == [
        (notes[PROJECT]["decision_id"], held.item_ref(capture_id, PROJECT))]
    assert [(r["decision_id"], r["item_ref"]) for r in rejected] == [
        (notes[PERSONAL]["decision_id"], held.item_ref(capture_id, PERSONAL))]
    assert {r["label_source"] for r in applied + rejected} == {"steward"}
    assert json.loads(applied[0]["details_json"])["action"] == "add"
    assert json.loads(rejected[0]["details_json"])["action"] == "ignore"


def test_an_explored_admission_gets_the_verdict_on_its_explore_decision(tmp_path):
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    engine, _jev = _engine(tmp_path, FakeJev(_fails_privacy_for(PROJECT)), explore=True)

    _worker(tmp_path, ledger, engine).run(apply_candidates=False)

    note = _notes(ledger, capture_id)[PROJECT]
    assert note["arm"] == "explore_admit"
    rows = [r for r in _outcomes_of(tmp_path, "consolidation_applied") if r["decision_id"] == note["decision_id"]]
    assert [r["item_ref"] for r in rows] == [held.item_ref(capture_id, PROJECT)]


def test_held_candidates_get_no_verdict_until_released(tmp_path):
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    engine, _jev = _engine(tmp_path, FakeJev(_fails_privacy_for(PROJECT)))

    _worker(tmp_path, ledger, engine).run(apply_candidates=False)

    refs = {r["item_ref"] for r in _outcomes_of(tmp_path, "consolidation_applied")}
    assert refs == {held.item_ref(capture_id, PERSONAL)}


def test_an_applied_claim_links_back_so_lifecycle_labels_join_the_s3_decision(tmp_path):
    from memorymaster.core.lifecycle import transition_claim
    from memorymaster.decisions import outcomes as oc

    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    engine, _jev = _engine(tmp_path)
    service = _service(tmp_path)
    DreamWorker(ledger, service, _Extractor(), _Recording(), config=DreamConfig(), now=lambda: NOW,
                decision_engine=engine).run(apply_candidates=True)
    note = _notes(ledger, capture_id)[PROJECT]
    [application] = [a for a in _applications(ledger, capture_id) if a["candidate_id"] == PROJECT]
    claim_id = application["created_claim_id"]
    links = [r for r in _outcomes_of(tmp_path, oc.CLAIM_LINK_KIND) if r["item_ref"] == f"claim:{claim_id}"]
    assert [r["decision_id"] for r in links] == [note["decision_id"]]
    assert json.loads(links[0]["details_json"])["dream_ref"] == held.item_ref(capture_id, PROJECT)

    transition_claim(service.store, claim_id, "confirmed", "steward check", event_type="validator")
    oc.tail_lifecycle(DecisionLedger(tmp_path / "decisions.db"), service.store.db_path)

    [confirmed] = [r for r in _outcomes_of(tmp_path, "steward_confirmed") if r["item_ref"] == f"claim:{claim_id}"]
    assert (confirmed["decision_id"], confirmed["label_source"]) == (note["decision_id"], "steward")
    assert _outcomes_of(tmp_path, "held_later_confirmed") == []  # admitted, never held


def test_a_released_candidate_confirmed_later_records_held_later_confirmed(tmp_path):
    from memorymaster.core.lifecycle import transition_claim
    from memorymaster.decisions import outcomes as oc

    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _capture(ledger)
    engine, _jev = _engine(tmp_path, FakeJev(_fails_privacy_for(PROJECT)))
    service = _service(tmp_path)
    DreamWorker(ledger, service, _Extractor(), _Recording(), config=DreamConfig(), now=lambda: NOW,
                decision_engine=engine).run(apply_candidates=True)
    hold = _notes(ledger, capture_id)[PROJECT]
    decisions = DecisionLedger(tmp_path / "decisions.db")
    assert held.release(ledger, capture_id=capture_id, actor="operator", decisions=decisions) == 1
    DreamWorker(ledger, service, _NoCall(), _Recording(), config=DreamConfig(), now=lambda: NOW,
                decision_engine=engine).run(apply_candidates=True)

    ref = held.item_ref(capture_id, PROJECT)
    assert [r["decision_id"] for r in _outcomes_of(tmp_path, "consolidation_applied") if r["item_ref"] == ref] == [
        hold["decision_id"]]
    [application] = [a for a in _applications(ledger, capture_id) if a["candidate_id"] == PROJECT]
    claim_id = application["created_claim_id"]
    oc.tail_lifecycle(decisions, service.store.db_path)
    assert _outcomes_of(tmp_path, "held_later_confirmed") == []  # created, not yet confirmed

    transition_claim(service.store, claim_id, "confirmed", "steward check", event_type="validator")
    oc.tail_lifecycle(decisions, service.store.db_path)

    [later] = _outcomes_of(tmp_path, "held_later_confirmed")
    assert (later["decision_id"], later["item_ref"], later["label_source"]) == (hold["decision_id"], ref, "steward")
    oc.tail_lifecycle(decisions, service.store.db_path)  # idempotent
    assert len(_outcomes_of(tmp_path, "held_later_confirmed")) == 1




def test_personal_candidates_are_asked_the_personal_scope_question(tmp_path):
    """Ruling R8: operator preferences were held because the scope/privacy questions assume a project."""
    ledger = DreamLedger(tmp_path / "capture.db")
    _capture(ledger)
    engine, jev = _engine(tmp_path)

    _worker(tmp_path, ledger, engine).run(apply_candidates=False)

    by_scope = {call["payload"]["state"]["scope"]: call["payload"]["questions"] for call in jev.calls}
    assert set(by_scope) == {"project:test", "personal"}
    texts = {scope: {wire.split("::")[0]: question["instructions"] for wire, question in questions.items()}
             for scope, questions in by_scope.items()}
    assert texts["personal"]["ingest.scope"] == (
        "Is `candidate` about the operator's own stable preferences, working style, tools, environment or constraints?")
    assert texts["project:test"]["ingest.scope"] == (
        "Does `candidate` apply to the project or workspace named in `scope`?")
    for scope in texts:
        assert texts[scope]["ingest.privacy"] == ("Is `candidate` free of secrets or credentials and of personal "
                                                  "data about people other than the operator?")
        assert texts[scope]["ingest.usefulness"] == (
            "Would `candidate` help a coding agent in a future session with this operator or on this project?")
    sets = {row["scope"]: row["question_set_id"] for row in _decisions(tmp_path)}
    assert "ingest.scope@v2" in sets["personal"] and "ingest.scope@v1" not in sets["personal"]
    assert "ingest.scope@v1" in sets["project:test"] and "ingest.privacy@v2" in sets["project:test"]
