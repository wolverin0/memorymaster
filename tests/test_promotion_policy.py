"""T-1354: claims authored by ``llm-stop-hook`` are never auto-promoted to confirmed.

Operator decision 2026-10-13: a blind-labelled review rated 30% of the hook's
confirmed claims as obvious or low value, so they stay ``candidate``. Every
automatic promoter must honour the single policy in ``core.promotion_policy``;
an explicit operator/agent action may still confirm them. All tests use a
disposable SQLite database.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.promotion_policy import (
    ENV_NON_AUTO_PROMOTABLE_SOURCES,
    PromotionPolicyBlocked,
    auto_promotion_blocked,
    non_auto_promotable_sources,
)
from memorymaster.core.service import MemoryService
from memorymaster.govern import llm_steward
from memorymaster.govern.jobs import revalidation, validator
from memorymaster.govern.llm_steward import ExtractionResult, run_steward

HOOK = "llm-stop-hook"


@pytest.fixture()
def svc(tmp_path: Path, monkeypatch) -> MemoryService:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.delenv(ENV_NON_AUTO_PROMOTABLE_SOURCES, raising=False)
    monkeypatch.setattr(validator, "load_classifier", lambda: None)
    service = MemoryService(str(tmp_path / "policy.db"), workspace_root=str(tmp_path))
    service.init_db()
    return service


def _claim(svc, text, *, source_agent, subject=None, predicate=None, object_value=None,
           confidence=0.8, scope="project:x"):
    return svc.ingest(
        text=text,
        citations=[CitationInput(source="session://t", locator="turn", excerpt=text[:30])],
        subject=subject, predicate=predicate, object_value=object_value,
        scope=scope, confidence=confidence, source_agent=source_agent,
    ).id


def _get(svc, claim_id):
    return svc.store.get_claim(claim_id, include_citations=False)


def _status_of(svc, claim_id) -> str:
    return _get(svc, claim_id).status


def _make_stale(svc, claim_id) -> None:
    """Mimic a legacy row: confirmed before the policy existed, then decayed."""
    transition_claim(svc.store, claim_id, "confirmed", reason="legacy", event_type="transition",
                     event_payload={"actor": "operator"})
    transition_claim(svc.store, claim_id, "stale", reason="decay", event_type="decay")


# --- the policy point -------------------------------------------------------

def test_default_set_and_env_override(monkeypatch):
    monkeypatch.delenv(ENV_NON_AUTO_PROMOTABLE_SOURCES, raising=False)
    assert non_auto_promotable_sources() == frozenset({HOOK, "precompact-residual"})
    assert auto_promotion_blocked(HOOK)
    assert auto_promotion_blocked("precompact-residual")  # T-1633
    assert not auto_promotion_blocked("claude-session")
    assert not auto_promotion_blocked(None)

    monkeypatch.setenv(ENV_NON_AUTO_PROMOTABLE_SOURCES, "a-agent, b-agent")
    assert auto_promotion_blocked("b-agent") and not auto_promotion_blocked(HOOK)

    monkeypatch.setenv(ENV_NON_AUTO_PROMOTABLE_SOURCES, "")
    assert non_auto_promotable_sources() == frozenset()
    assert not auto_promotion_blocked(HOOK)


# --- lifecycle gateway -------------------------------------------------------

def test_transition_refuses_automatic_confirm_of_stop_hook_claim(svc):
    cid = _claim(svc, "stop hook candidate that must stay a candidate", source_agent=HOOK)
    with pytest.raises(PromotionPolicyBlocked):
        transition_claim(svc.store, cid, "confirmed", reason="auto", event_type="validator")
    with pytest.raises(PromotionPolicyBlocked):
        transition_claim(svc.store, cid, "confirmed", reason="cycle", event_type="transition",
                         event_payload={"actor": "automation"})
    assert _status_of(svc, cid) == "candidate"


def test_stale_stop_hook_claim_cannot_be_reconfirmed_automatically(svc):
    cid = _claim(svc, "stop hook claim that went stale and stays stale", source_agent=HOOK)
    _make_stale(svc, cid)
    with pytest.raises(PromotionPolicyBlocked):
        transition_claim(svc.store, cid, "confirmed", reason="jev", event_type="validator")
    assert _status_of(svc, cid) == "stale"


@pytest.mark.parametrize("actor", ["operator", "agent"])
def test_explicit_actor_can_still_confirm_a_stop_hook_claim(svc, actor):
    cid = _claim(svc, f"stop hook claim confirmed explicitly by {actor}", source_agent=HOOK)
    updated = transition_claim(svc.store, cid, "confirmed", reason="explicit", event_type="transition",
                               event_payload={"actor": actor})
    assert updated.status == "confirmed"


def test_other_authors_still_confirm_through_the_gateway(svc):
    cid = _claim(svc, "ordinary claim promoted by an automatic job", source_agent="claude-session")
    assert transition_claim(svc.store, cid, "confirmed", reason="auto", event_type="validator").status == "confirmed"


def test_policy_can_be_disabled_by_configuration(svc, monkeypatch):
    monkeypatch.setenv(ENV_NON_AUTO_PROMOTABLE_SOURCES, "")
    cid = _claim(svc, "stop hook claim when the policy set is cleared", source_agent=HOOK)
    assert transition_claim(svc.store, cid, "confirmed", reason="auto", event_type="validator").status == "confirmed"


# --- validator cycle ---------------------------------------------------------

def test_validator_leaves_stop_hook_candidate_and_still_promotes_others(svc):
    held = _claim(svc, "Release deadline is 2026-04-01 according to the stop hook",
                  source_agent=HOOK, subject="rel-a", predicate="deadline", object_value="2026-04-01")
    other = _claim(svc, "Release deadline is 2026-04-01 according to a session",
                   source_agent="claude-session", subject="rel-b", predicate="deadline",
                   object_value="2026-04-01")

    validator.run(svc.store, min_citations=1, min_score=0.5)
    validator.run(svc.store, min_citations=1, min_score=0.5)

    assert _status_of(svc, other) == "confirmed", "control claim must still be promoted"
    assert _status_of(svc, held) == "candidate", "stop-hook claim must not be confirmed or conflicted"


def test_held_stop_hook_candidates_do_not_starve_ordinary_ones(svc):
    """Five hook candidates outrank (confidence 0.95) the one session candidate (0.7);
    with limit=3 the old fetch-then-hold filled the window with held claims forever."""
    for i in range(5):
        _claim(svc, f"stop hook candidate number {i} with high confidence", source_agent=HOOK,
               subject=f"hook-{i}", predicate="p", object_value="v", confidence=0.95)
    ordinary = _claim(svc, "ordinary session candidate with lower confidence", source_agent="claude-session",
                      subject="ord", predicate="p", object_value="v", confidence=0.7)

    for _ in range(2):
        validator.run(svc.store, limit=3, min_citations=1, min_score=0.5)

    assert _status_of(svc, ordinary) == "confirmed"


def test_find_by_status_excludes_sources_before_the_limit(svc):
    for i in range(3):
        _claim(svc, f"hook candidate {i} filling the window", source_agent=HOOK, confidence=0.95)
    ordinary = _claim(svc, "ordinary candidate behind the hook ones", source_agent="claude-session",
                      confidence=0.5)
    _claim(svc, "candidate without any author", source_agent=None, confidence=0.4)

    plain = svc.store.find_by_status("candidate", limit=3)
    filtered = svc.store.find_by_status("candidate", limit=3, exclude_source_agents={HOOK})

    assert ordinary not in {c.id for c in plain}
    assert ordinary in {c.id for c in filtered} and len(filtered) == 2
    assert all(c.source_agent != HOOK for c in filtered)


def test_validator_revalidation_does_not_reconfirm_stale_stop_hook_claim(svc):
    cid = _claim(svc, "Stale stop hook claim offered for revalidation", source_agent=HOOK,
                 subject="rel-c", predicate="deadline", object_value="x")
    _make_stale(svc, cid)
    stale = _get(svc, cid)

    result = validator.run(svc.store, min_citations=1, min_score=0.1,
                           revalidation_claims=[stale], policy_mode="cadence")

    assert _status_of(svc, cid) == "stale"
    assert result["confirmed"] == 0
    assert result["held_by_source_policy"] == 1
    holds = [e for e in svc.store.list_events(claim_id=cid, event_type="validator", limit=20)
             if e.details == "promotion_blocked_source_policy"]
    assert len(holds) == 1


# --- jev revalidation selection ----------------------------------------------

def test_jev_revalidation_never_selects_stop_hook_stale_claims(svc):
    hook_id = _claim(svc, "stale stop hook claim jev must not be paid to judge", source_agent=HOOK)
    other_id = _claim(svc, "stale ordinary claim jev may judge", source_agent="claude-session")
    for cid in (hook_id, other_id):
        _make_stale(svc, cid)

    selection = revalidation._Selection(
        SimpleNamespace(store=svc.store, tenant_id=None), None, limit=10, backfill=False, summary={},
    )
    page_ids = {row[0] for row in selection._page(("", 0), 50)}

    assert other_id in page_ids and hook_id not in page_ids


# --- llm steward (raw SQL path) ----------------------------------------------

def test_llm_steward_leaves_stop_hook_candidates_unconfirmed(svc, monkeypatch):
    hook_id = _claim(svc, "stop hook candidate the llm would happily confirm", source_agent=HOOK)
    other_id = _claim(svc, "ordinary candidate the llm confirms", source_agent="claude-session")
    seen: list[int] = []

    def fake_extract(provider, api_key, model, claim_id, text, base_url="", key_rotator=None,
                     use_llm_provider=False):
        seen.append(claim_id)
        return ExtractionResult(
            claim_id=claim_id, original_text=text, action="confirm", raw_response="",
            extractions=[{"action": "confirm", "subject": f"s{claim_id}", "predicate": "p",
                          "object_value": "v", "confidence": 0.9}],
        )

    monkeypatch.setattr(llm_steward, "extract_claim", fake_extract)

    run_steward(svc.store.db_path, api_key="x", provider="gemini", limit=100, delay=0.0,
                auto_validate=False)

    assert seen == [other_id], "stop-hook candidates must not even reach the LLM"
    assert _status_of(svc, hook_id) == "candidate"
    assert _status_of(svc, other_id) == "confirmed"


def test_llm_steward_confirm_cas_refuses_stop_hook_claim_directly(svc):
    hook_id = _claim(svc, "stop hook candidate hit directly by the raw confirm", source_agent=HOOK)
    version = _get(svc, hook_id).version
    conn = sqlite3.connect(svc.store.db_path)
    try:
        ok = llm_steward._confirm_candidate_cas(
            conn, claim_id=hook_id, version=version, subject="s", predicate="p",
            object_value="v", confidence=0.9,
        )
        conn.commit()
    finally:
        conn.close()
    assert ok is False
    assert _status_of(svc, hook_id) == "candidate"


# --- structure policy (T-1591) -----------------------------------------------

def test_structure_policy_blocks_only_a_predicate_without_subject():
    from memorymaster.core.promotion_policy import (
        claim_auto_promotion_blocked,
        is_malformed_triple,
        structure_blocks_auto_promotion,
    )

    assert is_malformed_triple(None, "CONSTRAINT") and is_malformed_triple("  ", "PROJECT")
    assert structure_blocks_auto_promotion("", "PROJECT")
    for subject, predicate in [(None, None), ("ledger", None), ("ledger", "refuses"), (None, ""), ("  ", "  ")]:
        assert not structure_blocks_auto_promotion(subject, predicate), (subject, predicate)

    row = {"source_agent": "claude-session", "subject": None, "predicate": "PROJECT", "object_value": None}
    assert claim_auto_promotion_blocked(row)
    assert claim_auto_promotion_blocked({**row, "source_agent": HOOK, "subject": "s", "predicate": "p"})
    assert not claim_auto_promotion_blocked({**row, "subject": "s", "predicate": "p"})
    assert claim_auto_promotion_blocked(SimpleNamespace(**{**row, "subject": ""}))
