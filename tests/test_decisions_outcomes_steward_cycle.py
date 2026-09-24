"""Outcome joiners wired into the steward cycle (4.9.0 wave 2).

* R5: a ``steward_human_override:`` status change with no payload actor predates
  F-21 (automation wrote that prefix too), so it is ``unknown_actor``, never
  ``operator``.
* Only real status changes are lifecycle outcomes: a same-status bookkeeping
  event (a confidence write) or a proposal (``policy_decision``) is not.
* ``steward_cycle_outcomes`` tails lifecycle events every cycle and runs the
  ledger retention prune at most once a day; with no ledger it creates nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.core import lifecycle
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.decisions import outcomes as oc
from memorymaster.decisions.config import DecisionConfig
from memorymaster.decisions.ledger import DecisionLedger, DecisionRecord, ItemRecord, utc_iso


@pytest.mark.parametrize("event_type,details,payload,expected", [
    ("transition", "steward_human_override:approve", None, "unknown_actor"),
    ("transition", "steward_human_override:approve", {"source": "human_override"}, "unknown_actor"),
    ("supersession", "steward_human_override:approve", {}, "unknown_actor"),
    ("transition", "steward_human_override:approve", {"actor": "operator"}, "operator"),
    ("transition", "steward_automation:approve", None, "automation"),
])
def test_pre_f21_human_override_without_actor_is_unknown_actor(event_type, details, payload, expected):
    assert oc.label_source(event_type, details, payload) == expected


@pytest.mark.parametrize("event_type,from_status,to_status,details,expected", [
    ("confidence", "confirmed", "confirmed", "jev_revalidation:d1 validator_score=0.7", None),
    ("confidence", "stale", "stale", "validator_score=0.5", None),
    ("validator", "conflicted", "conflicted", "revalidation_remains_conflicted:4", None),
    ("policy_decision", "confirmed", "stale", "steward_proposal:stale", None),
    ("policy_decision", "candidate", "superseded", "steward_proposal:jev_superseded_candidate", None),
    ("validator", "stale", "confirmed", "jev_revalidation:d1", "revalidated"),
    ("decay", "confirmed", "stale", "decayed", "stale"),
    ("supersession", "confirmed", "superseded", "superseded by claim:9", "superseded"),
])
def test_only_real_status_changes_are_lifecycle_outcomes(event_type, from_status, to_status, details, expected):
    assert oc.lifecycle_kind(event_type, to_status, details, from_status=from_status) == expected


def test_three_argument_calls_keep_their_meaning():
    assert oc.lifecycle_kind("decay", "stale", "decayed") == "stale"
    assert oc.lifecycle_kind("audit", None, "steward_proposal_approved") == "proposal_approved"


@pytest.fixture()
def world(tmp_path: Path):
    service = MemoryService(tmp_path / "memory.db", workspace_root=tmp_path)
    service.init_db()
    claim = service.ingest("The dashboard reads the decisions ledger read-only for its Jev tab",
                           [CitationInput(source="test", locator="fixture")], scope="project:test")
    config = DecisionConfig.from_env({"MEMORYMASTER_DECISIONS_DB": str(tmp_path / "decisions.db"),
                                      "MEMORYMASTER_DECISIONS_STATE_RETENTION_DAYS": "30"})
    return service, claim, config


def _decide(config: DecisionConfig, claim_id: int, *, ts: datetime, decision_id: str = "d1") -> None:
    DecisionLedger(config.decisions_db).write_decision(
        DecisionRecord(decision_id=decision_id, ts=utc_iso(ts), surface="dedup", mode="live",
                       state_redacted='{"state":"redacted"}'),
        [ItemRecord(decision_id=decision_id, item_ref=f"claim:{claim_id}", item_kind="claim")],
    )


def test_proposals_and_confidence_writes_do_not_become_outcomes(world):
    service, claim, config = world
    _decide(config, claim.id, ts=datetime.now(timezone.utc) - timedelta(seconds=5))
    service.store.record_event(claim_id=claim.id, event_type="policy_decision", from_status="candidate",
                               to_status="superseded", details="steward_proposal:jev_superseded_candidate",
                               payload={"source": "jev", "decision": "superseded_candidate"})
    service.store.set_confidence(claim.id, 0.91, details="validator_score=0.910;citations=1")
    lifecycle.transition_claim(service.store, claim.id, "confirmed", reason="validated", event_type="validator")

    oc.tail_lifecycle(DecisionLedger(config.decisions_db), service.store.db_path)

    rows = DecisionLedger(config.decisions_db).query("SELECT kind, label_source FROM outcomes")
    assert [(r["kind"], r["label_source"]) for r in rows] == [("steward_confirmed", "steward")]


def test_steward_cycle_outcomes_without_a_ledger_creates_nothing(world):
    service, _claim, config = world

    result = oc.steward_cycle_outcomes(service.store.db_path, config=config)

    assert result == {"ledger": False, "lifecycle_outcomes": 0, "pruned": None}
    assert not Path(config.decisions_db).exists()


def test_steward_cycle_outcomes_tails_every_cycle_and_prunes_daily(world):
    service, claim, config = world
    now = datetime.now(timezone.utc)
    _decide(config, claim.id, ts=now - timedelta(days=40), decision_id="old")
    _decide(config, claim.id, ts=now - timedelta(seconds=5), decision_id="new")
    lifecycle.transition_claim(service.store, claim.id, "confirmed", reason="validated", event_type="validator")

    first = oc.steward_cycle_outcomes(service.store.db_path, config=config, now=now)
    second = oc.steward_cycle_outcomes(service.store.db_path, config=config, now=now + timedelta(hours=6))
    third = oc.steward_cycle_outcomes(service.store.db_path, config=config, now=now + timedelta(hours=25))

    assert first["ledger"] is True and first["lifecycle_outcomes"] == 2  # both decisions saw the event
    assert first["pruned"] == 1  # the 40-day-old redacted state, past the 30-day retention
    assert second["pruned"] is None and second["lifecycle_outcomes"] == 0
    assert third["pruned"] == 0
    states = {r["decision_id"]: r["state_redacted"]
              for r in DecisionLedger(config.decisions_db).query("SELECT decision_id, state_redacted FROM decisions")}
    assert states == {"old": None, "new": '{"state":"redacted"}'}
