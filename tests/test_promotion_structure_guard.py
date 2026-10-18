"""T-1591: loopback-object and malformed-triple claims are never auto-promoted.

``govern.jobs.extractor.infer_structure`` stamps ``server / ip_address / <first
IPv4 in the text>`` on any prose that mentions an address, so long legitimate
claims that merely cite 127.0.0.1 carry a loopback "fact" as their structured
object. Those must not reach ``confirmed`` through an automatic path. Only the
hole-in-the-middle triple (predicate without subject) is malformed: subject +
predicate with no object is the normal shape of dream-worker output and must
keep promoting. Operator/agent actors are never affected. All tests use a
disposable SQLite database; private IPs stay promotable on purpose (see
``tests/test_security_patterns.py::TestPrivateIPv4NotRedactedAtIngest``).
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

LOOPBACK_OBJECTS = [
    "127.0.0.1",
    "localhost",
    "LOCALHOST",
    "  127.0.0.1  ",
    "::1",
    "[::1]",
    "127.0.0.1:8080",
    "localhost:3003",
    "[::1]:8080",
    "http://localhost:3003/health",
    "https://127.0.0.1/",
    "127.4.5.6",
]
NOT_LOOPBACK_OBJECTS = [
    "10.0.0.5",
    "192.168.1.20",
    "0.0.0.0",
    "localhost.example.com",
    "mylocalhost",
    "https://code.example.com/docs",
    "1270.0.0.1",
    "v",
]


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

@pytest.mark.parametrize("obj", LOOPBACK_OBJECTS)
def test_transition_refuses_automatic_confirm_of_loopback_object(svc, obj):
    cid = _claim(svc, "Long prose that merely mentions a local address", subject="server",
                 predicate="ip_address", object_value=obj)
    with pytest.raises(PromotionPolicyBlocked):
        transition_claim(svc.store, cid, "confirmed", reason="auto", event_type="validator")
    with pytest.raises(PromotionPolicyBlocked):
        transition_claim(svc.store, cid, "confirmed", reason="cycle", event_type="transition",
                         event_payload={"actor": "automation"})
    assert _status_of(svc, cid) == "candidate"


@pytest.mark.parametrize("obj", NOT_LOOPBACK_OBJECTS)
def test_non_loopback_objects_still_confirm(svc, obj):
    cid = _claim(svc, f"Host fact pointing at {obj}", subject="server", predicate="ip_address",
                 object_value=obj)
    assert transition_claim(svc.store, cid, "confirmed", reason="auto", event_type="validator").status == "confirmed"


def test_loopback_subject_alone_does_not_block(svc):
    """Only the structured OBJECT is inspected; the loopback literal as subject is prose."""
    cid = _claim(svc, "localhost is mentioned as the subject only", subject="localhost",
                 predicate="runs", object_value="vite")
    assert transition_claim(svc.store, cid, "confirmed", reason="auto", event_type="validator").status == "confirmed"


@pytest.mark.parametrize("actor", ["operator", "agent"])
def test_explicit_actor_can_still_confirm_a_loopback_claim(svc, actor):
    cid = _claim(svc, f"Loopback claim confirmed explicitly by {actor}", subject="server",
                 predicate="ip_address", object_value="127.0.0.1")
    updated = transition_claim(svc.store, cid, "confirmed", reason="explicit", event_type="transition",
                               event_payload={"actor": actor})
    assert updated.status == "confirmed"


def test_predicate_without_subject_is_malformed_and_blocked(svc):
    cid = _claim(svc, "Category label landed in the predicate slot", subject=None,
                 predicate="CONSTRAINT")
    with pytest.raises(PromotionPolicyBlocked):
        transition_claim(svc.store, cid, "confirmed", reason="auto", event_type="validator")
    assert _status_of(svc, cid) == "candidate"
    assert transition_claim(svc.store, cid, "confirmed", reason="explicit", event_type="transition",
                            event_payload={"actor": "operator"}).status == "confirmed"


def test_whitespace_subject_counts_as_empty(svc):
    cid = _claim(svc, "Predicate with a blank subject", subject="   ", predicate="PROJECT")
    with pytest.raises(PromotionPolicyBlocked):
        transition_claim(svc.store, cid, "confirmed", reason="auto", event_type="validator")


@pytest.mark.parametrize("subject,predicate,object_value", [
    (None, None, None),                      # free-text claim: the common legitimate shape
    ("ledger", "refuses unconfirmed", None),  # dream-worker shape: no object
    ("ledger", None, None),                  # subject only
    ("server", "ip_address", "10.0.0.5"),    # full triple
])
def test_legitimate_shapes_are_not_blocked(svc, subject, predicate, object_value):
    cid = _claim(svc, f"Legit claim {subject}/{predicate}/{object_value}", subject=subject,
                 predicate=predicate, object_value=object_value)
    assert transition_claim(svc.store, cid, "confirmed", reason="auto", event_type="validator").status == "confirmed"


def test_stale_loopback_claim_cannot_be_reconfirmed_automatically(svc):
    cid = _claim(svc, "Legacy confirmed claim that decayed", subject="server",
                 predicate="ip_address", object_value="127.0.0.1")
    transition_claim(svc.store, cid, "confirmed", reason="legacy", event_type="transition",
                     event_payload={"actor": "operator"})
    transition_claim(svc.store, cid, "stale", reason="decay", event_type="decay")
    with pytest.raises(PromotionPolicyBlocked):
        transition_claim(svc.store, cid, "confirmed", reason="jev", event_type="validator")
    assert _status_of(svc, cid) == "stale"


# --- validator cycle ----------------------------------------------------------

def test_validator_holds_loopback_candidate_and_still_promotes_others(svc):
    held = _claim(svc, "Dev server notes mention 127.0.0.1 in passing", subject="server",
                  predicate="ip_address", object_value="127.0.0.1")
    other = _claim(svc, "Release deadline is 2026-04-01 according to a session",
                   subject="rel-b", predicate="deadline", object_value="2026-04-01")

    first = validator.run(svc.store, min_citations=1, min_score=0.5)
    second = validator.run(svc.store, min_citations=1, min_score=0.5)

    assert _status_of(svc, other) == "confirmed", "control claim must still be promoted"
    assert _status_of(svc, held) == "candidate", "loopback claim must not be confirmed or conflicted"
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


def test_llm_steward_confirm_cas_refuses_rewritten_loopback_object(svc):
    cid = _claim(svc, "Candidate the llm rewrote into a loopback fact")
    assert _cas_confirm(svc, cid, subject="server", predicate="ip_address", object_value="localhost:3003") is False
    assert _status_of(svc, cid) == "candidate"


def test_llm_steward_confirm_cas_judges_the_stored_triple_not_the_arriving_one(svc):
    """The CAS overwrites the triple, so a clean rewrite of a loopback candidate may confirm."""
    cid = _claim(svc, "Candidate that arrived with a loopback object", subject="server",
                 predicate="ip_address", object_value="127.0.0.1")
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

def test_a_loopback_conflict_winner_is_not_promoted_and_does_not_supersede_the_rival(svc):
    rival = _claim(svc, "the older confirmed host fact with less confidence", subject="host-a",
                   predicate="ip_address", object_value="10.0.0.5", confidence=0.3)
    transition_claim(svc.store, rival, "confirmed", reason="fixture")
    winner = _claim(svc, "the newer conflicted claim that only cites a local address", subject="host-a",
                    predicate="ip_address", object_value="127.0.0.1", confidence=0.95)
    transition_claim(svc.store, winner, "conflicted", reason="fixture")

    result = curation_drain.drain_conflicts(svc, apply=True)

    assert result["held_by_structure_policy"] == 1 and result["resolved_won"] == 0
    assert _status_of(svc, winner) == "conflicted"
    assert _status_of(svc, rival) == "confirmed", "rival must not be superseded by a promotion that will not happen"
