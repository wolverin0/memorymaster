"""Outcome joiners: turn usage detector, actor-aware lifecycle tail, skills and held releases.

Joiners only append to the decisions ledger; the authoritative database is opened
read-only and is byte-identical afterwards.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.decisions import outcomes as oc
from memorymaster.decisions.ledger import DecisionLedger, DecisionRecord, ItemRecord, utc_iso

T0 = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)

CLAIM_1 = "SQLite with WAL is the authoritative store and vectors only propose ids for rehydration"
CLAIM_2 = "Run the public demo test before touching the lifecycle"
CLAIM_3 = "The steward cycle promotes candidates only after citations are verified by the validator job"


def _ledger(tmp_path: Path) -> DecisionLedger:
    return DecisionLedger(tmp_path / "decisions.db")


def _decision(ledger, decision_id, *, surface="recall", session="s1", ts=T0, exposed=("claim:1", "claim:2"),
              items=("claim:1", "claim:2", "claim:3"), action=None):
    ledger.write_decision(
        DecisionRecord(decision_id=decision_id, ts=utc_iso(ts), surface=surface, mode="live", session_key=session,
                       action_taken=json.dumps(action if action is not None else list(exposed))),
        [ItemRecord(decision_id=decision_id, item_ref=ref, item_kind="claim", question_id="",
                    exposed=int(ref in exposed), delivered=int(ref in exposed)) for ref in items],
    )


LOOKUP = {
    "claim:1": ("mm-aa11", CLAIM_1),
    "claim:2": ("mm-bb22", CLAIM_2),
    "claim:3": ("mm-cc33", CLAIM_3),
}.get


# ---------------------------------------------------------------- detector ---

def test_detects_human_id_case_insensitively():
    assert oc.detect_usage("mm-aa11", "short", "per MM-AA11 we keep WAL") == "human_id"
    assert oc.detect_usage("mm-aa11", "short", "per mm-aa111 we keep WAL") is None


def test_detects_distinctive_eight_gram_in_assistant_text_and_tool_inputs():
    reply = "As noted: sqlite with WAL is the authoritative store, and vectors only propose ids."
    assert oc.detect_usage("mm-zz", CLAIM_1, reply) == "ngram"
    tool = json.dumps({"command": "grep 'only after citations are verified by the validator job' -r ."})
    assert oc.detect_usage("mm-zz", CLAIM_3, tool) == "ngram"
    assert oc.detect_usage("mm-zz", CLAIM_1, "WAL is great") is None


def test_stopword_only_eight_grams_are_not_distinctive():
    claim = "it is what it is and that is how it was for all of us"
    text = "well, it is what it is and that is how it was for all of us"
    assert oc.distinctive_ngrams(claim) == set()
    assert oc.detect_usage(None, claim, text) is None


def test_short_claims_only_match_by_human_id():
    assert oc.distinctive_ngrams("use WAL") == set()
    assert oc.detect_usage("mm-bb22", "use WAL", "we use WAL") is None


# ---------------------------------------------------------- turn usage (a) ---

def _turn(text="", tools=(), turn_id="t1", at=T0 + timedelta(minutes=1)):
    return {"turn_id": turn_id, "observed_at": utc_iso(at), "assistant_text": text, "tool_inputs": list(tools)}


def _record(ledger, turn, *, session="s1", **kwargs):
    """The joiner requires the turn's own timestamp (``observed_at``); tests take it from the turn."""
    return oc.record_turn_usage(ledger, session, turn, observed_at=turn["observed_at"], **kwargs)


def test_turn_usage_requires_the_turn_timestamp(tmp_path):
    ledger = _ledger(tmp_path)
    _decision(ledger, "d1")
    turn = _turn(text="mm-aa11")
    with pytest.raises(TypeError):
        oc.record_turn_usage(ledger, "s1", turn, lookup=LOOKUP)  # observed_at omitted on purpose
    assert oc.record_turn_usage(ledger, "s1", turn, observed_at="", lookup=LOOKUP) == 0
    assert oc.record_turn_usage(ledger, "s1", turn, observed_at="not a time", lookup=LOOKUP) == 0
    assert ledger.query("SELECT COUNT(*) AS n FROM outcomes")[0]["n"] == 0


def test_replaying_a_turn_is_idempotent_even_without_its_watermark(tmp_path):
    ledger = _ledger(tmp_path)
    _decision(ledger, "d1")
    turn = _turn(text="mm-aa11 and mm-bb22")
    assert _record(ledger, turn, lookup=LOOKUP) == 2
    ledger.set_watermark(oc._session_watermark("s1"), utc_iso(T0))  # e.g. a restored or rewound watermark
    assert _record(ledger, turn, lookup=LOOKUP) == 0  # same turn timestamp: the same outcome rows
    observed = {r["observed_at"] for r in ledger.query("SELECT observed_at FROM outcomes")}
    assert observed == {turn["observed_at"]}


def test_turn_usage_records_only_exposed_items_of_that_session(tmp_path):
    ledger = _ledger(tmp_path)
    _decision(ledger, "d1")  # exposes claim:1, claim:2
    _decision(ledger, "other", session="s2", exposed=("claim:1",))
    _decision(ledger, "future", ts=T0 + timedelta(hours=1))
    turn = _turn(text="Keeping MM-AA11 in mind. Also the steward cycle promotes candidates only after citations are "
                      "verified by the validator job.")
    assert _record(ledger, turn, lookup=LOOKUP) == 1
    rows = ledger.query("SELECT * FROM outcomes")
    assert [(r["decision_id"], r["item_ref"], r["kind"], r["value"], r["was_exposed"], r["label_source"])
            for r in rows] == [("d1", "claim:1", "used_in_turn", 1.0, 1, "detector")]
    assert rows[0]["lag_s"] == 60
    assert json.loads(rows[0]["details_json"]) == {"turn_id": "t1", "via": "human_id"}


def test_turn_usage_is_idempotent_and_watermarked(tmp_path):
    ledger = _ledger(tmp_path)
    _decision(ledger, "d1")
    turn = _turn(text="mm-aa11 and mm-bb22")
    assert _record(ledger, turn, lookup=LOOKUP) == 2
    assert _record(ledger, turn, lookup=LOOKUP) == 0
    older = _turn(text="mm-aa11", turn_id="t0", at=T0 + timedelta(seconds=30))
    assert _record(ledger, older, lookup=LOOKUP) == 0
    later = _turn(text="mm-aa11", turn_id="t2", at=T0 + timedelta(minutes=5))
    assert _record(ledger, later, lookup=LOOKUP) == 1
    assert len(ledger.query("SELECT * FROM outcomes")) == 3


def test_turn_usage_uses_tool_inputs_and_session_start_decisions(tmp_path):
    ledger = _ledger(tmp_path)
    _decision(ledger, "boot", surface="session", exposed=("claim:3",), items=("claim:3",))
    _decision(ledger, "skills", surface="skills", exposed=("claim:1",), items=("claim:1",))
    turn = _turn(tools=[{"pattern": "only after citations are verified by the validator job"}, "mm-aa11"])
    assert _record(ledger, turn, lookup=LOOKUP) == 1
    assert ledger.query("SELECT decision_id FROM outcomes")[0]["decision_id"] == "boot"


def test_turn_usage_survives_lookup_failures(tmp_path):
    ledger = _ledger(tmp_path)
    _decision(ledger, "d1")

    def broken(ref):
        raise RuntimeError("db gone")

    assert _record(ledger, _turn(text="mm-aa11"), lookup=broken) == 0


# ------------------------------------------------------ lifecycle tail (b) ---

@pytest.fixture()
def service(tmp_path):
    from memorymaster.core.models import CitationInput
    from memorymaster.core.service import MemoryService

    workspace = tmp_path / "ws"
    workspace.mkdir()
    svc = MemoryService(workspace / "memory.db", workspace_root=workspace)
    svc.init_db()
    claims = [svc.ingest(text=text, citations=[CitationInput(source="test", locator="fixture")],
                         scope="project:test", source_agent="fixture")
              for text in (CLAIM_1, CLAIM_2, CLAIM_3)]
    return svc, workspace / "memory.db", claims


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_lifecycle_tail_maps_events_with_actor_aware_labels(tmp_path, service):
    from memorymaster.core.lifecycle import transition_claim

    svc, db, (c1, c2, c3) = service
    ledger = _ledger(tmp_path)
    decision_ts = datetime.now(timezone.utc) - timedelta(seconds=5)
    refs = [f"claim:{c.id}" for c in (c1, c2, c3)]
    _decision(ledger, "d1", ts=decision_ts, exposed=refs[:2], items=refs)

    transition_claim(svc.store, c1.id, "confirmed", reason="validator promote", event_type="validator")
    transition_claim(svc.store, c1.id, "stale", reason="confidence fell below threshold", event_type="decay")
    transition_claim(svc.store, c1.id, "confirmed", reason="jev_revalidation:d9", event_type="validator")
    transition_claim(svc.store, c2.id, "confirmed", reason="validator promote", event_type="validator")
    svc.store.record_event(claim_id=c2.id, event_type="audit", details="steward_proposal_approved",
                           payload={"source": "human_override", "proposal_event_id": 1})
    svc.store.record_event(claim_id=c2.id, event_type="audit", details="steward_proposal_rejected",
                           payload={"source": "human_override", "actor": "operator", "proposal_event_id": 2})
    svc.store.record_event(claim_id=c3.id, event_type="audit", details="steward_proposal_approved",
                           payload={"source": "human_override", "actor": "automation", "proposal_event_id": 3})
    # Status changes applied by a proposal resolution carry the actor as a details prefix (F-21).
    transition_claim(svc.store, c3.id, "conflicted", reason="steward_automation:approve", event_type="transition")
    transition_claim(svc.store, c3.id, "stale", reason="steward_human_override:approve", event_type="transition")

    before = _sha(db)
    inserted = oc.tail_lifecycle(ledger, db)
    assert _sha(db) == before  # read-only: the authoritative store is untouched
    got = sorted((r["item_ref"], r["kind"], r["label_source"], r["was_exposed"])
                 for r in ledger.query("SELECT * FROM outcomes"))
    assert got == sorted([
        (refs[0], "steward_confirmed", "steward", 1),
        (refs[0], "stale", "automation", 1),
        (refs[0], "revalidated", "jev", 1),
        (refs[1], "steward_confirmed", "steward", 1),
        (refs[1], "proposal_approved", "unattributed_override", 1),
        (refs[1], "proposal_rejected", "operator", 1),
        (refs[2], "proposal_approved", "automation", 0),
        (refs[2], "conflicted", "automation", 0),
        # R5: a pre-F-21 human-override prefix without a payload actor is not a human label.
        (refs[2], "stale", "unknown_actor", 0),
    ])
    assert inserted == 9
    assert oc.tail_lifecycle(ledger, db) == 0
    assert int(ledger.get_watermark(oc.LIFECYCLE_WATERMARK)) > 0


def test_lifecycle_tail_ignores_events_before_the_decision(tmp_path, service):
    from memorymaster.core.lifecycle import transition_claim

    svc, db, (c1, _c2, _c3) = service
    transition_claim(svc.store, c1.id, "confirmed", reason="early", event_type="validator")
    ledger = _ledger(tmp_path)
    _decision(ledger, "late-decision", ts=datetime.now(timezone.utc) + timedelta(hours=1),
              exposed=[f"claim:{c1.id}"], items=[f"claim:{c1.id}"])
    assert oc.tail_lifecycle(ledger, db) == 0
    assert ledger.query("SELECT COUNT(*) AS n FROM outcomes")[0]["n"] == 0


def test_lifecycle_tail_on_missing_db_returns_zero(tmp_path):
    assert oc.tail_lifecycle(_ledger(tmp_path), tmp_path / "missing.db") == 0
    assert not (tmp_path / "missing.db").exists()


@pytest.mark.parametrize("event_type,details,payload,expected", [
    ("decay", "x", None, "automation"),
    ("staleness", "scheduled", None, "automation"),
    ("validator", "promote", None, "steward"),
    # R5: before F-21 automation wrote this prefix too; without a payload actor it proves nothing.
    ("transition", "steward_human_override:approve", None, "unknown_actor"),
    ("transition", "steward_human_override:approve", {"actor": "operator"}, "operator"),
    # An explicit payload actor wins over a details prefix.
    ("transition", "steward_human_override:approve", {"actor": "automation"}, "automation"),
    ("audit", "steward_proposal_approved", {"source": "automation", "actor": "automation"}, "automation"),
    ("audit", "steward_proposal_rejected", {"source": "human_override", "actor": "operator"}, "operator"),
    # A legacy resolution with neither prefix nor actor is never human ground truth.
    ("audit", "steward_proposal_approved", {"source": "human_override"}, "unattributed_override"),
    ("transition", "manual", None, "unknown"),
    ("validator", "jev_revalidation:abc", None, "jev"),
    ("audit", "steward_proposal_approved", {"source": "jev"}, "jev"),
    ("audit", "steward_proposal_approved", {"actor": "robot"}, "unknown"),
    # Track R status-change details carry the actor as a prefix (F-21).
    ("transition", "steward_automation:approve", None, "automation"),
    ("supersession", "steward_automation:approve", None, "automation"),
    ("supersession", "steward_human_override:approve", None, "unknown_actor"),
    # Jev's own revalidation stays "jev" even if S1 records an actor.
    ("validator", "jev_revalidation:abc", {"actor": "automation"}, "jev"),
    ("validator", "jev_revalidation:abc", {"actor": "operator"}, "jev"),
    ("transition", "jev_revalidation:abc", None, "jev"),
    # An operator resolving a Jev proposal is a human label for Jev.
    ("audit", "steward_proposal_approved", {"source": "jev", "actor": "operator"}, "operator"),
])
def test_label_source_rules(event_type, details, payload, expected):
    assert oc.label_source(event_type, details, payload) == expected


# --------------------------------------------------------- skills / held ---

def test_skill_invocation_joins_suggestions(tmp_path):
    ledger = _ledger(tmp_path)
    _decision(ledger, "sk1", surface="skills", exposed=("skill:7",), items=("skill:7", "skill:8"))
    at = T0 + timedelta(minutes=2)
    assert oc.record_skill_invocation(ledger, session_key="s1", skill_ref="skill:7", observed_at=utc_iso(at)) == 1
    assert oc.record_skill_invocation(ledger, session_key="s1", skill_ref="skill:9", observed_at=utc_iso(at)) == 1
    assert oc.record_skill_invocation(ledger, session_key="s1", skill_ref="skill:7", observed_at=utc_iso(at)) == 0
    kinds = {(r["item_ref"], r["kind"], r["was_exposed"]) for r in ledger.query("SELECT * FROM outcomes")}
    assert kinds == {("skill:7", "skill_invoked", 1), ("skill:9", "skill_invoked_unsuggested", 0)}
    assert oc.record_skill_invocation(ledger, session_key="nobody", skill_ref="skill:7") == 0


def test_held_release_and_later_confirmation(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.write_decision(DecisionRecord(decision_id="i1", ts=utc_iso(T0), surface="ingest", mode="live",
                                         action_taken=json.dumps("hold")),
                          [ItemRecord(decision_id="i1", item_ref="candidate:abc", item_kind="candidate")])
    ledger.write_decision(DecisionRecord(decision_id="i2", ts=utc_iso(T0), surface="ingest", mode="live",
                                         action_taken=json.dumps("admit")),
                          [ItemRecord(decision_id="i2", item_ref="candidate:abc", item_kind="candidate")])
    at = utc_iso(T0 + timedelta(days=1))
    assert oc.record_held_release(ledger, item_ref="candidate:abc", actor="operator", observed_at=at,
                                  confirmed=True) == 2
    rows = ledger.query("SELECT decision_id, kind, label_source FROM outcomes ORDER BY kind")
    assert [(r["decision_id"], r["kind"], r["label_source"]) for r in rows] == [
        ("i1", "held_later_confirmed", "operator"), ("i1", "held_released", "operator")]
    assert oc.record_held_release(ledger, item_ref="candidate:none", actor="operator") == 0


def test_claim_lookup_reads_authoritative_db_read_only(service):
    svc, db, (c1, _c2, _c3) = service
    before = _sha(db)
    lookup = oc.claim_lookup(db)
    human_id, text = lookup(f"claim:{c1.id}")
    assert human_id == c1.human_id and text == CLAIM_1
    assert lookup("claim:999999") is None
    assert lookup("skill:1") is None and lookup("claim:x") is None
    assert oc.claim_lookup(db.parent / "missing.db")("claim:1") is None
    assert _sha(db) == before


def test_lifecycle_tail_first_run_skips_history_before_any_decision(tmp_path, service):
    from memorymaster.core.lifecycle import transition_claim

    svc, db, (c1, c2, _c3) = service
    transition_claim(svc.store, c1.id, "confirmed", reason="history", event_type="validator")
    ledger = _ledger(tmp_path)
    assert oc.tail_lifecycle(ledger, db) == 0 and not ledger.exists()  # no ledger: nothing joined or created
    ledger.set_watermark("test:marker", "1")  # the ledger exists but holds no decision yet
    assert oc.tail_lifecycle(ledger, db) == 0  # no decisions yet: jump the watermark to the head
    head = int(ledger.get_watermark(oc.LIFECYCLE_WATERMARK))
    assert head > 0
    _decision(ledger, "d-new", ts=datetime.now(timezone.utc) - timedelta(seconds=2),
              exposed=[f"claim:{c2.id}"], items=[f"claim:{c2.id}"])
    transition_claim(svc.store, c2.id, "confirmed", reason="after", event_type="validator")
    assert oc.tail_lifecycle(ledger, db) == 1
    assert int(ledger.get_watermark(oc.LIFECYCLE_WATERMARK)) > head


def test_lifecycle_tail_first_run_starts_near_the_first_decision(tmp_path, service):
    from memorymaster.core.lifecycle import transition_claim

    svc, db, (c1, _c2, _c3) = service
    ledger = _ledger(tmp_path)
    _decision(ledger, "d-early", ts=datetime.now(timezone.utc) - timedelta(seconds=2),
              exposed=[f"claim:{c1.id}"], items=[f"claim:{c1.id}"])
    transition_claim(svc.store, c1.id, "confirmed", reason="after", event_type="validator")
    assert oc.tail_lifecycle(ledger, db) == 1


def _fail_outcome_writes(tmp_path: Path, failing: bool) -> None:
    """Make the ledger's outcome inserts fail at the SQLite level (test-only trigger)."""
    import sqlite3

    conn = sqlite3.connect(tmp_path / "decisions.db")
    try:
        if failing:
            conn.execute("CREATE TRIGGER test_fail_outcomes BEFORE INSERT ON outcomes "
                         "BEGIN SELECT RAISE(ABORT, 'disk full'); END")
        else:
            conn.execute("DROP TRIGGER test_fail_outcomes")
        conn.commit()
    finally:
        conn.close()


def test_turn_usage_watermark_waits_for_a_successful_outcome_write(tmp_path):
    ledger = _ledger(tmp_path)
    _decision(ledger, "d1")
    turn = _turn(text="mm-aa11 and mm-bb22")
    _fail_outcome_writes(tmp_path, True)
    assert _record(ledger, turn, lookup=LOOKUP) == 0
    _fail_outcome_writes(tmp_path, False)
    assert _record(ledger, turn, lookup=LOOKUP) == 2  # the retry is not skipped
    assert _record(ledger, turn, lookup=LOOKUP) == 0


def test_lifecycle_watermark_waits_for_a_successful_outcome_write(tmp_path, service):
    from memorymaster.core.lifecycle import transition_claim

    svc, db, (c1, _c2, _c3) = service
    ledger = _ledger(tmp_path)
    ref = f"claim:{c1.id}"
    _decision(ledger, "d1", ts=datetime.now(timezone.utc) - timedelta(seconds=5), exposed=[ref], items=[ref])
    transition_claim(svc.store, c1.id, "confirmed", reason="validator promote", event_type="validator")
    _fail_outcome_writes(tmp_path, True)
    assert oc.tail_lifecycle(ledger, db) == 0
    _fail_outcome_writes(tmp_path, False)
    assert oc.tail_lifecycle(ledger, db) == 1
    assert oc.tail_lifecycle(ledger, db) == 0


def test_lifecycle_tail_survives_a_corrupt_watermark(tmp_path, service):
    from memorymaster.core.lifecycle import transition_claim

    svc, db, (c1, _c2, _c3) = service
    ledger = _ledger(tmp_path)
    ref = f"claim:{c1.id}"
    _decision(ledger, "d1", ts=datetime.now(timezone.utc) - timedelta(seconds=5), exposed=[ref], items=[ref])
    transition_claim(svc.store, c1.id, "confirmed", reason="validator promote", event_type="validator")
    ledger.set_watermark(oc.LIFECYCLE_WATERMARK, "not-a-number")
    assert oc.tail_lifecycle(ledger, db) == 1  # re-derived from the first decision; idempotent
    assert int(ledger.get_watermark(oc.LIFECYCLE_WATERMARK)) > 0
    assert oc.tail_lifecycle(ledger, db) == 0


def test_joiners_never_create_an_absent_ledger(tmp_path, service):
    from memorymaster.core.lifecycle import transition_claim

    svc, db, (c1, _c2, _c3) = service
    transition_claim(svc.store, c1.id, "confirmed", reason="validator promote", event_type="validator")
    path = tmp_path / "absent" / "decisions.db"
    ledger = DecisionLedger(path)
    assert _record(ledger, _turn(text="mm-aa11"), lookup=LOOKUP) == 0
    assert oc.tail_lifecycle(ledger, db) == 0
    assert oc.record_skill_invocation(ledger, session_key="s1", skill_ref="skill:1") == 0
    assert oc.record_held_release(ledger, item_ref="candidate:1", actor="operator") == 0
    assert oc.record_outcome(ledger, "d1", "claim:1", "requery") == 0
    assert not path.exists() and not path.parent.exists()


def _fail_reads(monkeypatch, needle: str) -> list[int]:
    """Make ledger reads whose SQL contains ``needle`` fail (writes keep working)."""
    failures: list[int] = []
    real = DecisionLedger._read

    def flaky(self, sql, params=()):
        if needle in sql and not failures:
            failures.append(1)
            return None
        return real(self, sql, params)

    monkeypatch.setattr(DecisionLedger, "_read", flaky)
    return failures


def test_turn_usage_keeps_its_watermark_when_the_exposure_read_fails(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    _decision(ledger, "d1")
    turn = _turn(text="mm-aa11 and mm-bb22")
    failures = _fail_reads(monkeypatch, "JOIN decision_items")
    assert _record(ledger, turn, lookup=LOOKUP) == 0
    assert failures == [1]
    assert ledger.get_watermark(oc._session_watermark("s1")) is None  # not advanced past the turn
    assert _record(ledger, turn, lookup=LOOKUP) == 2  # the retry is not skipped
    assert _record(ledger, turn, lookup=LOOKUP) == 0


def test_lifecycle_keeps_its_watermark_when_the_decision_item_read_fails(tmp_path, service, monkeypatch):
    from memorymaster.core.lifecycle import transition_claim

    svc, db, (c1, _c2, _c3) = service
    ledger = _ledger(tmp_path)
    ref = f"claim:{c1.id}"
    _decision(ledger, "d1", ts=datetime.now(timezone.utc) - timedelta(seconds=5), exposed=[ref], items=[ref])
    transition_claim(svc.store, c1.id, "confirmed", reason="validator promote", event_type="validator")
    assert oc.tail_lifecycle(ledger, db) == 1
    mark = ledger.get_watermark(oc.LIFECYCLE_WATERMARK)
    transition_claim(svc.store, c1.id, "stale", reason="confidence fell below threshold", event_type="decay")
    failures = _fail_reads(monkeypatch, "FROM decision_items i")
    assert oc.tail_lifecycle(ledger, db) == 0
    assert failures == [1]
    assert ledger.get_watermark(oc.LIFECYCLE_WATERMARK) == mark  # not advanced past the unjoined event
    assert oc.tail_lifecycle(ledger, db) == 1
    kinds = sorted(r["kind"] for r in ledger.query("SELECT kind FROM outcomes"))
    assert kinds == ["stale", "steward_confirmed"]


def test_lifecycle_first_run_does_not_skip_history_when_the_ledger_read_fails(tmp_path, service, monkeypatch):
    from memorymaster.core.lifecycle import transition_claim

    svc, db, (c1, _c2, _c3) = service
    ledger = _ledger(tmp_path)
    ref = f"claim:{c1.id}"
    _decision(ledger, "d1", ts=datetime.now(timezone.utc) - timedelta(seconds=5), exposed=[ref], items=[ref])
    transition_claim(svc.store, c1.id, "confirmed", reason="validator promote", event_type="validator")
    failures = _fail_reads(monkeypatch, "SELECT 1 AS present FROM decisions")
    assert oc.tail_lifecycle(ledger, db) == 0
    assert failures == [1]
    assert ledger.get_watermark(oc.LIFECYCLE_WATERMARK) is None  # not jumped to the latest event
    assert oc.tail_lifecycle(ledger, db) == 1


def test_skill_and_held_joiners_return_zero_when_the_ledger_read_fails(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    _decision(ledger, "sk1", surface="skills", exposed=("skill:7",), items=("skill:7",))
    at = utc_iso(T0 + timedelta(minutes=2))
    _fail_reads(monkeypatch, "surface = 'skills'")
    assert oc.record_skill_invocation(ledger, session_key="s1", skill_ref="skill:7", observed_at=at) == 0
    assert oc.record_skill_invocation(ledger, session_key="s1", skill_ref="skill:7", observed_at=at) == 1
    monkeypatch.undo()
    _fail_reads(monkeypatch, "surface = 'ingest'")
    assert oc.record_held_release(ledger, item_ref="candidate:x", actor="operator") == 0
