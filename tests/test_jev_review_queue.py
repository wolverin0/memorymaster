"""Operator review of Jev decisions: information-driven weekly queue and operator labels.

The queue must surface the decisions an operator learns most from (answers near a
threshold, Jev vs legacy or steward disagreement, paraphrase disagreement, one per
score decile), never repeat what was already labelled, and read the ledger without
writing it.  A label is an append-only ``operator_review`` outcome recorded through
``decisions.outcomes.record_outcome`` with ``label_source='operator'``.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from memorymaster.decisions.ledger import DecisionLedger, DecisionRecord, ItemRecord, OutcomeRecord, utc_iso
from memorymaster.surfaces import jev_review as jr

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def decision(ledger, did, *, ts, surface="recall", mode="live", legacy='["claim:1"]', jev='["claim:1"]',
             taken=None, fallback=None, thresholds=None, state=None, items=(), propensity=1.0, arm="policy",
             cost=0.0001, outcome="ok", attempt=1, session=None):
    ok = ledger.write_decision(DecisionRecord(
        decision_id=did, ts=utc_iso(ts), surface=surface, mode=mode, fallback_reason=fallback,
        transport_outcome=outcome, attempt_count=attempt, latency_ms=420, engine_ms=450, cost_usd=cost,
        legacy_action=legacy, jev_action=jev, action_taken=taken if taken is not None else (jev or legacy),
        thresholds_json=json.dumps(thresholds or {}), state_redacted=json.dumps(state) if state else None,
        chosen_propensity=propensity, exploration_arm=arm, session_key=session,
    ), list(items))
    assert ok


def item(did, ref, q, answer, *, version=1, exposed=1, kind="claim"):
    return ItemRecord(decision_id=did, item_ref=ref, item_kind=kind, question_id=q, question_version=version,
                      answer=str(answer), probabilities_json=json.dumps({"noul": answer}), exposed=exposed,
                      delivered=exposed)


def outcome(ledger, did, ref, kind, *, source="steward", at=None, details=None):
    ledger.record_outcomes([OutcomeRecord(did, ref, kind, 1.0, was_exposed=1, label_source=source,
                                          observed_at=utc_iso(at or NOW - timedelta(hours=1)),
                                          details_json=json.dumps(details) if details else None)])


def seed(ledger, now=NOW):
    """A week of decisions with one clear example of every informative category."""
    t = now - timedelta(days=2)
    decision(ledger, "near", ts=t, surface="revalidate", legacy='"keep_stale"', jev='"keep_stale"',
             thresholds={"lifecycle.still_valid@v1": {"accept": 0.85, "low": 0.2}},
             state={"state": {"claim": {"text": "The API listens on port 8080"}}, "subjects": {}},
             items=[item("near", "claim:10", "lifecycle.still_valid", 0.84)])
    decision(ledger, "disagree", ts=t + timedelta(minutes=1), legacy='["claim:1","claim:2"]',
             jev='["claim:2","claim:1"]', items=[item("disagree", "claim:2", "recall.relevant", 0.62),
                                                  item("disagree", "claim:1", "recall.relevant", 0.3)])
    decision(ledger, "para", ts=t + timedelta(minutes=2), surface="dedup", legacy='"none"', jev='"none"',
             items=[item("para", "pair:3-4", "memory.supersedes", 0.9, kind="pair"),
                    item("para", "pair:3-4", "memory.supersedes_alt", 0.1, kind="pair")])
    decision(ledger, "steward", ts=t + timedelta(minutes=3), surface="ingest", legacy='"admit"', jev='"admit"',
             items=[item("steward", "claim:77", "ingest.usefulness", 0.93)])
    outcome(ledger, "steward", "claim:77", "archived", source="steward", at=now - timedelta(hours=1))
    # Filler: recall decisions agreeing with legacy, answers spread over every decile.
    for index in range(40):
        answer = round(0.025 + index * 0.024, 3)
        decision(ledger, f"fill{index:02d}", ts=t + timedelta(minutes=10 + index),
                 items=[item(f"fill{index:02d}", f"claim:{100 + index}", "recall.usable_evidence", answer)])
    # Outside the weekly window, and decisions Jev never answered.
    decision(ledger, "old", ts=now - timedelta(days=10), surface="revalidate", legacy='"keep_stale"',
             jev='"keep_stale"', thresholds={"lifecycle.still_valid@v1": {"accept": 0.85}},
             items=[item("old", "claim:11", "lifecycle.still_valid", 0.85)])
    decision(ledger, "fallback", ts=t + timedelta(hours=2), mode="shadow", fallback="timeout", outcome="timeout",
             jev=None, items=[ItemRecord(decision_id="fallback", item_ref="claim:5", question_id="")])


@pytest.fixture()
def ledger(tmp_path):
    ledger = DecisionLedger(tmp_path / "decisions.db")
    seed(ledger)
    return ledger


def _by_id(queue):
    return {entry["decision_id"]: entry for entry in queue}


def test_queue_covers_every_informative_category_within_the_week(ledger):
    queue = jr.select_review_queue(ledger, now=NOW, days=7, size=20)
    assert 0 < len(queue) <= 20
    found = _by_id(queue)
    assert "near_threshold" in found["near"]["reasons"]
    assert "legacy_disagreement" in found["disagree"]["reasons"]
    assert "paraphrase_disagreement" in found["para"]["reasons"]
    assert "steward_disagreement" in found["steward"]["reasons"]
    assert "old" not in found and "fallback" not in found
    deciles = {min(int(float(e["answer"]) * 10), 9) for e in queue}
    assert deciles == set(range(10)), deciles  # every score decile, not the 20 most recent
    assert sum(1 for e in queue if "score_decile" in e["reasons"]) >= 5
    near = found["near"]
    assert near["question"].startswith("Is `claim` still a correct statement")
    assert near["threshold_distance"] == pytest.approx(0.01)
    assert near["thresholds"] == {"accept": 0.85, "low": 0.2}
    assert near["state"] == {"claim": {"text": "The API listens on port 8080"}}
    assert found["para"]["paraphrase"] == {"question_id": "memory.supersedes_alt", "answer": 0.1}
    assert len({(e["decision_id"], e["item_ref"]) for e in queue}) == len(queue)


def test_queue_is_deterministic_and_skips_labelled_items(ledger):
    first = jr.select_review_queue(ledger, now=NOW, size=20)
    assert first == jr.select_review_queue(ledger, now=NOW, size=20)
    target = _by_id(first)["near"]
    result = jr.record_review_label(DecisionLedger(ledger.path), decision_id="near", item_ref=target["item_ref"],
                                    question_id=target["question_id"], verdict="correct", now=NOW)
    assert result["ok"] is True
    again = jr.select_review_queue(ledger, now=NOW, size=20)
    assert "near" not in _by_id(again)


def test_skip_rows_are_never_queued(tmp_path):
    """A ``skip:`` decision did not ask Jev: even an answered item logged on one is nothing to review."""
    ledger = DecisionLedger(tmp_path / "decisions.db")
    decision(ledger, "skipped", ts=NOW - timedelta(days=1), fallback="skip:no_candidates", outcome="not_sent",
             attempt=0, legacy='["claim:1"]', jev='["claim:9"]', taken='["claim:1"]',
             thresholds={"recall.usable_evidence@v1": {"include": 0.5}},
             items=[item("skipped", "claim:9", "recall.usable_evidence", 0.51)])
    decision(ledger, "asked", ts=NOW - timedelta(days=1, minutes=1),
             items=[item("asked", "claim:8", "recall.usable_evidence", 0.9)])
    queue = jr.select_review_queue(ledger, now=NOW)
    assert [entry["decision_id"] for entry in queue] == ["asked"]


def test_label_is_an_append_only_operator_outcome_with_implied_truth(ledger):
    jr.record_review_label(ledger, decision_id="near", item_ref="claim:10", question_id="lifecycle.still_valid",
                           verdict="incorrect", now=NOW)
    rows = ledger.query("SELECT * FROM outcomes WHERE decision_id = 'near'")
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == jr.REVIEW_KIND == "operator_review"
    assert row["label_source"] == "operator"
    assert row["value"] == 0.0
    details = json.loads(row["details_json"])
    # 0.84 reads as "yes"; the operator says Jev is wrong, so the truth is "no".
    assert details == {"question_id": "lifecycle.still_valid", "question_version": 1, "verdict": "incorrect",
                       "answer": 0.84, "truth": 0}
    jr.record_review_label(ledger, decision_id="near", item_ref="claim:10", question_id="lifecycle.still_valid",
                           verdict="correct", now=NOW + timedelta(seconds=1))
    later = ledger.query("SELECT value, details_json FROM outcomes WHERE decision_id = 'near' ORDER BY observed_at")
    assert [r["value"] for r in later] == [0.0, 1.0]
    assert json.loads(later[1]["details_json"])["truth"] == 1


@pytest.mark.parametrize("kwargs", [
    {"decision_id": "missing", "item_ref": "claim:10", "question_id": "lifecycle.still_valid", "verdict": "correct"},
    {"decision_id": "near", "item_ref": "claim:999", "question_id": "lifecycle.still_valid", "verdict": "correct"},
    {"decision_id": "near", "item_ref": "claim:10", "question_id": "lifecycle.still_valid", "verdict": "maybe"},
    {"decision_id": "fallback", "item_ref": "claim:5", "question_id": "", "verdict": "correct"},
])
def test_label_rejects_unknown_items_and_verdicts(ledger, kwargs):
    with pytest.raises(ValueError):
        jr.record_review_label(ledger, now=NOW, **kwargs)
    assert ledger.query("SELECT COUNT(*) AS n FROM outcomes WHERE kind = 'operator_review'")[0]["n"] == 0


def test_reads_never_create_or_modify_the_ledger(tmp_path):
    absent = tmp_path / "absent.db"
    view = jr.ReadOnlyLedger(absent)
    assert jr.select_review_queue(view, now=NOW) == []
    assert not absent.exists()
    with pytest.raises(ValueError):
        jr.record_review_label(DecisionLedger(absent), decision_id="x", item_ref="claim:1", question_id="q",
                               verdict="correct", now=NOW)
    assert not absent.exists()
    ledger = DecisionLedger(tmp_path / "d.db")
    seed(ledger)
    before = hashlib.sha256(ledger.path.read_bytes()).hexdigest()
    view = jr.ReadOnlyLedger(ledger.path)
    assert jr.select_review_queue(view, now=NOW)
    assert jr.metrics_payload(view, now=NOW)["surfaces"]
    assert hashlib.sha256(ledger.path.read_bytes()).hexdigest() == before
    with pytest.raises(Exception):
        view.query("DELETE FROM watermarks")


@pytest.mark.parametrize("folder", ["pct%41dir", "hash#dir"])
def test_read_only_view_opens_paths_with_uri_special_characters(tmp_path, folder):
    """'%' and '#' are URI syntax: unquoted, '%41' opens another file and '#' drops mode=ro and creates one."""
    (tmp_path / folder).mkdir()
    ledger = DecisionLedger(tmp_path / folder / "decisions.db")
    decision(ledger, "a", ts=NOW - timedelta(hours=1))
    rows = jr.ReadOnlyLedger(ledger.path).query("SELECT decision_id FROM decisions")
    assert [row["decision_id"] for row in rows] == ["a"]
    assert sorted(path.name for path in tmp_path.iterdir()) == [folder]  # no stray truncated-path file


def test_pair_questions_are_graded_by_proposal_verdicts_not_lifecycle_events(tmp_path):
    """'conflicted' after memory.contradicts=yes (or 'superseded' after memory.supersedes=yes) agrees with Jev."""
    ledger = DecisionLedger(tmp_path / "decisions.db")
    t = NOW - timedelta(days=1)
    for did, question, kind in (("c", "memory.contradicts", "conflicted"), ("s", "memory.supersedes", "superseded"),
                                ("r", "memory.contradicts", "proposal_rejected")):
        decision(ledger, did, ts=t, surface="dedup", legacy='"none"', jev='"none"',
                 items=[item(did, f"pair:{did}", question, 0.9, kind="pair")])
        outcome(ledger, did, f"pair:{did}", kind, source="steward")
    queue = jr.select_review_queue(ledger, now=NOW, size=20)
    assert {e["decision_id"] for e in queue if "steward_disagreement" in e["reasons"]} == {"r"}


def test_metrics_payload_adds_configured_modes_cap_and_today_cost(ledger):
    from memorymaster.decisions.config import DecisionConfig

    config = DecisionConfig.from_env({"MEMORYMASTER_JEV_MODE": "shadow", "MEMORYMASTER_JEV_RECALL": "live",
                                      "MEMORYMASTER_JEV_DAILY_USD_CAP": "1.5"})
    decision(ledger, "today", ts=NOW - timedelta(hours=1), cost=0.25,
             items=[item("today", "claim:3", "recall.usable_evidence", 0.5)])
    payload = jr.metrics_payload(jr.ReadOnlyLedger(ledger.path), config, now=NOW)
    assert payload["ok"] is True
    assert payload["configured_modes"]["recall"] == "live"
    assert payload["configured_modes"]["ingest"] == "shadow"
    assert payload["daily_usd_cap"] == 1.5
    assert payload["cost_today_usd"] == pytest.approx(0.25)
    recall = payload["surfaces"]["recall"]
    assert recall["engine_ms"]["p50"] == 450 and recall["latency_ms"]["p95"] == 420
    json.dumps(payload)  # the dashboard writes it with the plain JSON encoder
