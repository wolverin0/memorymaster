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
