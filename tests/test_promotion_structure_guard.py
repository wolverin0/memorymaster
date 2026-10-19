"""T-1591: a predicate without a subject is never auto-promoted.

A predicate with no subject is a shifted field (a category label such as
CONSTRAINT / PROJECT in the predicate slot), not a triple. Subject + predicate
with no object is NOT malformed: it is the normal shape of dream-worker output
(313 confirmed claims) and must keep promoting, as must free-text claims and
loopback/private objects (the loopback noise is fixed at the source, see
``tests/test_extractor_infer_structure.py``). Operator/agent actors are never
affected. All tests use a disposable SQLite database.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.promotion_policy import (
    ENV_NON_AUTO_PROMOTABLE_SOURCES,
    PromotionPolicyBlocked,
)
from memorymaster.core.service import MemoryService
from memorymaster.govern import llm_steward
from memorymaster.govern.jobs import curation_drain, validator

@pytest.fixture()
def svc(tmp_path: Path, monkeypatch) -> MemoryService:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.delenv(ENV_NON_AUTO_PROMOTABLE_SOURCES, raising=False)
    monkeypatch.setattr(validator, "load_classifier", lambda: None)
    service = MemoryService(str(tmp_path / "structure.db"), workspace_root=str(tmp_path))
    service.init_db()
    return service


def _claim(svc, text, *, subject=None, predicate=None, object_value=None,
           source_agent="claude-session", confidence=0.8, scope="project:x"):
    return svc.ingest(
        text=text,
        citations=[CitationInput(source="session://t", locator="turn", excerpt=text[:30])],
        subject=subject, predicate=predicate, object_value=object_value,
        scope=scope, confidence=confidence, source_agent=source_agent,
    ).id


def _status_of(svc, claim_id) -> str:
    return svc.store.get_claim(claim_id, include_citations=False).status


# --- lifecycle gateway --------------------------------------------------------

def test_predicate_without_subject_is_malformed_and_blocked(svc):
    cid = _claim(svc, "Category label landed in the predicate slot", subject=None,
                 predicate="CONSTRAINT")
    with pytest.raises(PromotionPolicyBlocked):
        transition_claim(svc.store, cid, "confirmed", reason="auto", event_type="validator")
    assert _status_of(svc, cid) == "candidate"
    assert transition_claim(svc.store, cid, "confirmed", reason="explicit", event_type="transition",
                            event_payload={"actor": "operator"}).status == "confirmed"


def test_stale_malformed_claim_cannot_be_reconfirmed_automatically(svc):
    cid = _claim(svc, "Legacy confirmed claim that decayed", subject=None, predicate="PROJECT")
    transition_claim(svc.store, cid, "confirmed", reason="legacy", event_type="transition",
                     event_payload={"actor": "operator"})
    transition_claim(svc.store, cid, "stale", reason="decay", event_type="decay")
    with pytest.raises(PromotionPolicyBlocked):
        transition_claim(svc.store, cid, "confirmed", reason="jev", event_type="validator")
    assert _status_of(svc, cid) == "stale"


def test_whitespace_subject_counts_as_empty(svc):
    cid = _claim(svc, "Predicate with a blank subject", subject="   ", predicate="PROJECT")
    with pytest.raises(PromotionPolicyBlocked):
        transition_claim(svc.store, cid, "confirmed", reason="auto", event_type="validator")


@pytest.mark.parametrize("subject,predicate,object_value", [
    (None, None, None),                      # free-text claim: the common legitimate shape
    ("ledger", "refuses unconfirmed", None),  # dream-worker shape: no object
    ("ledger", None, None),                  # subject only
    ("server", "ip_address", "10.0.0.5"),    # full triple
    ("server", "ip_address", "127.0.0.1"),   # loopback is fixed in the extractor, not blocked here
])
def test_legitimate_shapes_are_not_blocked(svc, subject, predicate, object_value):
    cid = _claim(svc, f"Legit claim {subject}/{predicate}/{object_value}", subject=subject,
                 predicate=predicate, object_value=object_value)
    assert transition_claim(svc.store, cid, "confirmed", reason="auto", event_type="validator").status == "confirmed"


# --- validator cycle ----------------------------------------------------------

def test_validator_holds_malformed_candidate_and_still_promotes_others(svc):
    held = _claim(svc, "Marketing note whose category label landed in the predicate",
                  subject=None, predicate="CONSTRAINT")
    other = _claim(svc, "Release deadline is 2026-04-01 according to a session",
                   subject="rel-b", predicate="deadline", object_value="2026-04-01")

    first = validator.run(svc.store, min_citations=1, min_score=0.5)
    second = validator.run(svc.store, min_citations=1, min_score=0.5)

    assert _status_of(svc, other) == "confirmed", "control claim must still be promoted"
    assert _status_of(svc, held) == "candidate", "malformed claim must not be confirmed or conflicted"
    assert first["held_by_structure_policy"] == 1 and second["held_by_structure_policy"] == 1
    holds = [e for e in svc.store.list_events(claim_id=held, event_type="validator", limit=20)
             if e.details == "promotion_blocked_structure_policy"]
    assert len(holds) == 1, "hold is audited once, not once per cycle"


def test_validator_keeps_promoting_subject_predicate_without_object(svc):
    cid = _claim(svc, "Ledger refuses to start an old card that has no receipt",
                 subject="ledger", predicate="refuses unconfirmed old cards")
    validator.run(svc.store, min_citations=1, min_score=0.5)
    assert _status_of(svc, cid) == "confirmed"


# --- llm steward (raw SQL path) ------------------------------------------------

def _cas_confirm(svc, claim_id, **fields):
    version = svc.store.get_claim(claim_id, include_citations=False).version
    conn = sqlite3.connect(svc.store.db_path)
    try:
        ok = llm_steward._confirm_candidate_cas(
            conn, claim_id=claim_id, version=version, confidence=0.9, **fields,
        )
        conn.commit()
    finally:
        conn.close()
    return ok


def test_llm_steward_confirm_cas_judges_the_stored_triple_not_the_arriving_one(svc):
    """The CAS overwrites the triple, so a clean rewrite of a malformed candidate may confirm."""
    cid = _claim(svc, "Candidate that arrived with a predicate and no subject", subject=None,
                 predicate="PROJECT")
    assert _cas_confirm(svc, cid, subject="dev-workflow", predicate="runs on", object_value=None) is True
    assert _status_of(svc, cid) == "confirmed"


def test_llm_steward_confirm_cas_refuses_malformed_rewrite(svc):
    cid = _claim(svc, "Candidate the llm rewrote with a predicate and no subject")
    assert _cas_confirm(svc, cid, subject="", predicate="PROJECT", object_value=None) is False


def test_llm_steward_confirm_cas_still_confirms_ordinary_rewrite(svc):
    cid = _claim(svc, "Ordinary candidate")
    assert _cas_confirm(svc, cid, subject="s", predicate="p", object_value=None) is True
    assert _status_of(svc, cid) == "confirmed"


# --- curation drain -------------------------------------------------------------

def test_a_malformed_conflict_winner_is_not_promoted_and_does_not_supersede_the_rival(svc):
    rival = _claim(svc, "the older confirmed fact with less confidence", subject="host-a",
                   predicate="ip_address", object_value="10.0.0.5", confidence=0.3)
    transition_claim(svc.store, rival, "confirmed", reason="fixture")
    winner = _claim(svc, "the newer conflicted claim with a shifted predicate", subject=None,
                    predicate="ip_address", object_value="10.0.0.6", confidence=0.95)
    transition_claim(svc.store, winner, "conflicted", reason="fixture",
                     event_type="validator")
    # A subject-less claim has no (subject, predicate) rival; the drain finds it in the audit trail.
    svc.store.record_event(claim_id=winner, event_type="validator",
                           details=f"conflicts_with_confirmed_claim:{rival}")

    result = curation_drain.drain_conflicts(svc, apply=True)

    assert result["held_by_structure_policy"] == 1 and result["resolved_won"] == 0
    assert _status_of(svc, winner) == "conflicted"
    assert _status_of(svc, rival) == "confirmed", "rival must not be superseded by a promotion that will not happen"
