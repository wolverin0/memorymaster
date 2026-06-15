"""Tests for rule-shaped claims (v3.21.0-R1a).

R1a is the storage + retrieval slice: build, ingest, parse, and query
rule-shaped claims. The auto-correction-extraction (dream_bridge) and
context_hook injection are R1b (deferred fast-follow).

Storage convention (see memorymaster/rules.py): a rule is a claim with
claim_type='rule', predicate='applies_when', subject=trigger, and a JSON
{trigger, action, rationale} in object_value. No schema change.
"""
from __future__ import annotations

import json

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.knowledge.rules import (
    RULE_CLAIM_TYPE,
    RULE_PREDICATE,
    build_rule_fields,
    is_rule,
    parse_rule,
    render_rule_text,
)
from memorymaster.core.service import MemoryService


@pytest.fixture
def svc(tmp_path):
    s = MemoryService(tmp_path / "rules.db", workspace_root=tmp_path)
    s.init_db()
    return s


def _ingest_rule(svc, trigger, action, rationale=""):
    return svc.ingest(
        **build_rule_fields(trigger, action, rationale),
        citations=[CitationInput(source="test://rule", locator="l", excerpt="e")],
        source_agent="rule-test",
    )


# ---------------------------------------------------------------------------
# build_rule_fields / render
# ---------------------------------------------------------------------------


def test_build_rule_fields_shape():
    fields = build_rule_fields("adding a new dependency", "run the integration tests", "unit tests miss migration breaks")
    assert fields["claim_type"] == RULE_CLAIM_TYPE
    assert fields["predicate"] == RULE_PREDICATE
    assert fields["subject"] == "adding a new dependency"
    payload = json.loads(fields["object_value"])
    assert payload == {
        "trigger": "adding a new dependency",
        "action": "run the integration tests",
        "rationale": "unit tests miss migration breaks",
    }
    assert "When adding a new dependency, run the integration tests." in fields["text"]


def test_build_rule_fields_requires_trigger_and_action():
    with pytest.raises(ValueError, match="trigger and an action"):
        build_rule_fields("", "do something")
    with pytest.raises(ValueError, match="trigger and an action"):
        build_rule_fields("some trigger", "")


def test_render_rule_text_without_rationale():
    txt = render_rule_text("X happens", "do Y", "")
    assert txt == "When X happens, do Y."


# ---------------------------------------------------------------------------
# Round-trip: ingest -> parse
# ---------------------------------------------------------------------------


def test_rule_round_trip(svc):
    claim = _ingest_rule(svc, "deploying on Friday", "wait until Monday", "Friday deploys cause weekend pages")
    fetched = svc.store.get_claim(claim.id)
    assert is_rule(fetched)
    parsed = parse_rule(fetched)
    assert parsed["trigger"] == "deploying on Friday"
    assert parsed["action"] == "wait until Monday"
    assert parsed["rationale"] == "Friday deploys cause weekend pages"
    assert parsed["claim_id"] == claim.id


def test_parse_rule_returns_none_for_non_rule(svc):
    claim = svc.ingest(
        text="the database is PostgreSQL",
        citations=[CitationInput(source="s", locator="l")],
        source_agent="t",
    )
    assert parse_rule(svc.store.get_claim(claim.id)) is None


def test_parse_rule_tolerates_malformed_payload():
    # Simulate a rule claim whose object_value isn't valid JSON.
    class FakeClaim:
        claim_type = RULE_CLAIM_TYPE
        id = 1
        subject = "trigger text"
        object_value = "not json {{{"
        text = "When trigger text, do something."

    parsed = parse_rule(FakeClaim())
    assert parsed is not None
    assert parsed["trigger"] == "trigger text"  # falls back to subject
    assert parsed["action"] == ""  # no crash


# ---------------------------------------------------------------------------
# query_rules
# ---------------------------------------------------------------------------


def test_query_rules_returns_matching_rule(svc):
    _ingest_rule(svc, "adding a new npm dependency", "pin the exact version", "floating versions broke CI")
    _ingest_rule(svc, "writing a database migration", "test rollback too", "forward-only migrations stranded prod once")

    hits = svc.query_rules("new dependency", limit=5, allow_sensitive=True)
    assert hits, "expected at least one rule hit"
    triggers = [h["trigger"] for h in hits]
    assert any("dependency" in t for t in triggers)
    # Each hit carries the structured shape
    top = hits[0]
    assert "trigger" in top and "action" in top and "rationale" in top


def test_query_rules_excludes_non_rule_claims(svc):
    _ingest_rule(svc, "handling user input", "validate at the boundary", "injection risk")
    svc.ingest(
        text="user input validation is handled by pydantic in this repo",
        citations=[CitationInput(source="s", locator="l")],
        source_agent="t",
    )
    hits = svc.query_rules("user input validation", limit=10, allow_sensitive=True)
    # Every hit must be a parsed rule (has trigger/action), never a fact claim
    assert all(h.get("action") is not None for h in hits)
    assert all("claim_id" in h for h in hits)
    # The fact claim's text must not appear as a rule action
    assert all("pydantic" not in (h["action"] or "") for h in hits)


def test_query_rules_empty_when_no_rules(svc):
    svc.ingest(
        text="just a regular fact claim about the stack",
        citations=[CitationInput(source="s", locator="l")],
        source_agent="t",
    )
    hits = svc.query_rules("anything", limit=5, allow_sensitive=True)
    assert hits == []


# ---------------------------------------------------------------------------
# Safety: rule with a URL in its action survives a steward cycle
# (validates the predicate-gated-validator assumption that lets us store
#  the JSON payload in object_value without a schema change)
# ---------------------------------------------------------------------------


def test_rule_with_url_action_survives_run_cycle(svc):
    claim = _ingest_rule(
        svc,
        "deploying the dashboard",
        "bind to 127.0.0.1 not http://0.0.0.0:8765",
        "non-loopback bind without auth is exposed",
    )
    # run_cycle exercises the deterministic validators. A rule's predicate
    # ('applies_when') must not trigger url/ip value-validation on the JSON
    # object_value. The claim must survive intact.
    svc.run_cycle(policy_mode="legacy", min_citations=1, min_score=0.5)
    fetched = svc.store.get_claim(claim.id)
    parsed = parse_rule(fetched)
    assert parsed is not None
    assert parsed["action"] == "bind to 127.0.0.1 not http://0.0.0.0:8765"
    # object_value is still valid JSON (not rewritten by a value-validator)
    json.loads(fetched.object_value)
