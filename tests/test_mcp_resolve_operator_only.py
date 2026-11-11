"""G-3b: an MCP session (an agent) cannot approve an operator-only proposal it can file itself."""
from __future__ import annotations

import json

import pytest

from memorymaster.core import lifecycle
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.steward import list_steward_proposals

CIT = [CitationInput(source="test://g3b")]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    monkeypatch.delenv("MEMORYMASTER_MCP_OPERATOR_RESOLVE", raising=False)
    svc = MemoryService(tmp_path / "g3b.db", workspace_root=tmp_path)
    svc.init_db()
    return svc


def _cross_identity_proposal(svc):
    y = svc.ingest("The zorblax cache invalidation strategy is the pelican strategy", CIT, scope="project:g3",
                   source_agent="claude-session")
    lifecycle.transition_claim(svc.store, y.id, "confirmed", reason="fixture", event_type="validator")
    svc.ingest("Unrelated note: lunch is at noon on fridays in the main hall", CIT, scope="project:g3",
               source_agent="evil-x", supersedes=[y.id])
    [p] = [p for p in list_steward_proposals(svc, limit=100) if p["claim_id"] == y.id]
    assert p["payload"].get("cross_identity") is True
    return y, p


def _mcp(svc, action, claim_id):
    from memorymaster.surfaces.mcp_server import resolve_steward_proposal as mcp_resolve

    return mcp_resolve(action=action, db=str(svc.store.db_path), workspace=str(svc.workspace_root), claim_id=claim_id)


def _status(svc, cid):
    return svc.store.get_claim(cid, include_citations=False).status


def test_agent_cannot_approve_cross_identity_proposal(env):
    y, _p = _cross_identity_proposal(env)
    out = _mcp(env, "approve", y.id)
    assert out["ok"] is False and out["code"] == "PROPOSAL_OPERATOR_ONLY", out
    assert "dashboard" in json.dumps(out) and "--actor operator" in json.dumps(out)
    assert _status(env, y.id) == "confirmed"


def test_agent_may_reject_it(env):
    y, _p = _cross_identity_proposal(env)
    out = _mcp(env, "reject", y.id)
    assert out["ok"] is True and out["result"]["status"] == "rejected", out


def test_operator_opt_in_allows_approval_and_labels_operator(env, monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_MCP_OPERATOR_RESOLVE", "1")
    y, _p = _cross_identity_proposal(env)
    out = _mcp(env, "approve", y.id)
    assert out["ok"] is True and out["result"]["applied"] is True, out
    assert _status(env, y.id) == "superseded"


def test_agent_approval_of_ordinary_proposal_is_labelled_agent(env):
    old = env.ingest("The nightly backup job starts at two in the morning", CIT, scope="project:g3",
                     source_agent="a1")
    lifecycle.transition_claim(env.store, old.id, "confirmed", reason="fixture", event_type="validator")
    env.ingest("The nightly backup job starts at three in the morning", CIT, scope="project:g3",
               source_agent="a1", supersedes_claim_id=old.id)
    out = _mcp(env, "approve", old.id)
    assert out["ok"] is True and out["result"]["applied"] is True, out
    audit = [e for e in env.store.list_events(claim_id=old.id, event_type="audit")
             if e.details == "steward_proposal_approved"]
    payload = json.loads(audit[0].payload_json)
    assert payload["actor"] == "agent" and payload["source"] == "agent"


def test_a_refused_approval_is_audited_and_the_proposal_stays_pending(env):
    from memorymaster.recall.retrieval import pending_supersession_ids
    from memorymaster.govern.ingest_governance import has_live_proposal

    y, _p = _cross_identity_proposal(env)
    # the promotion-gate view: the replacement is still a candidate, so recall does not hide the target yet
    gate_before = set(pending_supersession_ids(env, use_cache=False, deliverable_only=False))
    pending_before = set(pending_supersession_ids(env, use_cache=False))
    assert y.id in gate_before
    for _ in range(2):
        assert _mcp(env, "approve", y.id)["code"] == "PROPOSAL_OPERATOR_ONLY"
    refused = [e for e in env.store.list_events(claim_id=y.id, event_type="audit")
               if e.details == "steward_proposal_refused"]
    assert len(refused) == 2
    payload = json.loads(refused[0].payload_json)
    assert payload["actor"] == "agent" and payload["reason"] == "operator_only" and payload["source"] == "agent"
    assert isinstance(payload["proposal_event_id"], int)
    # still pending everywhere, ranked down exactly as before
    assert [p for p in list_steward_proposals(env, limit=100) if p["claim_id"] == y.id][0]["status"] == "pending"
    assert has_live_proposal(env.store, y.id)
    assert set(pending_supersession_ids(env, use_cache=False)) == pending_before
    assert set(pending_supersession_ids(env, use_cache=False, deliverable_only=False)) == gate_before
    assert _status(env, y.id) == "confirmed"

