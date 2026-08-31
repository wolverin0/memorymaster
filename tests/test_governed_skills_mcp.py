"""MCP parity tests for governed skill operations.

The agent surface may propose candidates and recall confirmed skills, while the
steward-only review tool remains explicit. Export targets only a supplied
MemoryMaster staging root and never an operator-owned global skill directory.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import memorymaster.core.access_control as access_control
import memorymaster.surfaces.mcp_server as mcp_server
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.knowledge.rule_miner import rule_fingerprint
from memorymaster.knowledge.rule_observations import record_rule_observation
from memorymaster.knowledge.rules import build_rule_fields


def _payload() -> dict:
    return {
        "schema": "personal-skill-v1",
        "slug": "bounded-mcp-review",
        "title": "Bounded MCP review",
        "when_to_use": "When reviewing an MCP integration candidate.",
        "when_not_to_use": "When no MCP surface changed.",
        "inputs": ["candidate diff"],
        "prerequisites": ["disposable database"],
        "workflow": ["Inspect authorization.", "Run focused tests."],
        "decision_rules": ["Reject unauthorized scope access."],
        "expected_output": "A bounded MCP review report.",
        "validation": ["Confirm denied calls fail before tool bodies."],
        "pitfalls": ["A registered tool may still lack a policy."],
        "recovery": ["Disable the tool and repair its policy."],
        "quality_scores": {
            "recurrence": 15,
            "reusability": 15,
            "executability": 15,
            "validation": 15,
            "safety": 15,
        },
    }


@pytest.fixture
def mcp_skill_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "mcp-skills.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    access_control._agent_roles.clear()
    monkeypatch.setattr(access_control, "_loaded", True)
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()
    trigger = "reviewing an MCP integration candidate"
    action = "run authorization and focused tests"
    rule = service.ingest(
        **build_rule_fields(trigger, action, "registration alone is not proof"),
        citations=[CitationInput(source="verbatim", locator="mcp-correction")],
        scope="project:workspace",
        source_agent="rule-miner",
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rule_stats "
            "(rule_fingerprint TEXT PRIMARY KEY, correction_count INTEGER NOT NULL, "
            "last_mined TEXT NOT NULL, confidence_at_last_mine REAL)"
        )
        conn.execute(
            "INSERT INTO rule_stats(rule_fingerprint, correction_count, last_mined) VALUES (?, 2, ?)",
            (rule_fingerprint(trigger, action), "2026-08-07T00:00:00+00:00"),
        )
        fingerprint = rule_fingerprint(trigger, action)
        for index in range(3):
            record_rule_observation(
                conn,
                rule_fingerprint=fingerprint,
                provider="codex",
                root_session_id=f"mcp-root-{index}",
                project_scope="project:workspace",
                source_ref=f"mcp:{index}",
                evidence_hash=f"{index + 1:064x}",
            )
    yield str(db), str(workspace), rule.id, tmp_path
    access_control._agent_roles.clear()


def test_mcp_skill_round_trip(mcp_skill_env) -> None:
    db, workspace, rule_id, tmp_path = mcp_skill_env
    inputs = mcp_server.skill_inputs(scope="project:workspace", db=db, workspace=workspace)
    assert inputs["rows"] == 1

    proposed = mcp_server.skill_propose(
        payload_json=json.dumps(_payload()),
        supporting_claim_ids=[rule_id],
        scope="project:workspace",
        db=db,
        workspace=workspace,
    )
    assert proposed["created"] is True
    assert mcp_server.skill_recall(
        "MCP review", scope_allowlist="project:workspace", db=db, workspace=workspace
    )["rows"] == 0

    approved = mcp_server.skill_review(
        proposed["claim_id"], "approve", db=db, workspace=workspace
    )
    assert approved["approved"] is True
    assert mcp_server.skill_recall(
        "MCP review", scope_allowlist="project:workspace", db=db, workspace=workspace
    )["rows"] == 1

    staging = tmp_path / "mcp-staging"
    exported = mcp_server.skill_export(
        staging_root=str(staging),
        scope_allowlist="project:workspace",
        db=db,
        workspace=workspace,
    )
    assert exported["exported"] == 1
    assert Path(exported["files"][0]).is_relative_to(staging)
