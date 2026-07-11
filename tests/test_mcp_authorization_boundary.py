"""Red contracts for the MCP authorization boundary (MM-SEC-01).

These tests intentionally describe the required team-safe behavior before the
request-context implementation exists.  They stay strict-xfailed until the
MCP boundary derives identity and intersects caller scopes with policy.
"""
from __future__ import annotations

import sqlite3

import pytest

import memorymaster.core.access_control as access_control
import memorymaster.surfaces.mcp_server as mcp_server
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService


AUDIT_BASELINE = pytest.mark.xfail(
    strict=True,
    reason="audit baseline MM-SEC-01: MCP request authorization is not enforced",
)


@pytest.fixture(autouse=True)
def isolated_mcp_state(monkeypatch: pytest.MonkeyPatch):
    """Keep global role/rate state from leaking between adversarial tests."""
    mcp_server._INGEST_RATE_BUCKETS.clear()
    monkeypatch.setenv("MM_INGEST_RATE_LIMIT_PER_MIN", "0")
    monkeypatch.setattr(mcp_server, "_ENV_DEFAULT_PROJECT_SCOPE", "")
    monkeypatch.setattr(mcp_server, "_ENV_DEFAULT_WORKSPACE", "")
    monkeypatch.setattr(mcp_server, "_ENV_QUERY_INCLUDE_LEGACY_PROJECT", False)
    access_control._agent_roles.clear()
    access_control._loaded = True
    yield
    mcp_server._INGEST_RATE_BUCKETS.clear()
    access_control._agent_roles.clear()
    access_control._loaded = False


def _init_mcp_db(tmp_path) -> tuple[str, str]:
    db = str(tmp_path / "authorization.db")
    workspace = str(tmp_path / "alpha")
    (tmp_path / "alpha").mkdir()
    mcp_server.init_db(db=db, workspace=workspace)
    return db, workspace


def _claim_count(db: str) -> int:
    with sqlite3.connect(db) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0])


def _was_denied(call) -> bool:
    try:
        result = call()
    except PermissionError:
        return True
    return isinstance(result, dict) and result.get("ok") is False


@AUDIT_BASELINE
def test_reader_cannot_ingest_by_spoofing_source_agent(tmp_path) -> None:
    """An attribution field cannot replace the authenticated MCP principal."""
    db, workspace = _init_mcp_db(tmp_path)
    access_control.set_role("mcp-session", access_control.Role.READER)
    access_control.set_role("forged-writer", access_control.Role.WRITER)

    denied = _was_denied(
        lambda: mcp_server.ingest_claim(
            text="A reader must not create this governed claim",
            sources_json='["test://authorization"]',
            source_agent="forged-writer",
            db=db,
            workspace=workspace,
        )
    )

    assert denied, "the reader session must fail even with a writer-shaped source_agent"
    assert _claim_count(db) == 0, "a denied write may not mutate domain state"


@AUDIT_BASELINE
def test_list_claims_is_restricted_to_the_workspace_scope(tmp_path) -> None:
    """A project-scoped MCP client must not enumerate another project."""
    db, workspace = _init_mcp_db(tmp_path)
    svc = MemoryService(db, workspace_root=tmp_path)
    svc.ingest(
        "alpha authorization boundary marker",
        [CitationInput(source="test://alpha")],
        scope="project:alpha",
    )
    svc.ingest(
        "beta authorization boundary marker",
        [CitationInput(source="test://beta")],
        scope="project:beta",
    )

    result = mcp_server.list_claims(db=db, workspace=workspace, limit=20)
    scopes = {claim["scope"] for claim in result["claims"]}

    assert scopes <= {"project:alpha", "global"}
