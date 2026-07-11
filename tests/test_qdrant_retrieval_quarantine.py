"""Adversarial contracts for quarantining non-authoritative Qdrant reads."""
from __future__ import annotations

import json
from typing import Any

import pytest

import memorymaster.core.access_control as access_control
import memorymaster.recall.qdrant_backend as qdrant_backend
import memorymaster.recall.query_classifier as query_classifier
import memorymaster.surfaces.mcp_server as mcp_server
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.surfaces.cli import main


@pytest.fixture(autouse=True)
def isolated_auth_state(monkeypatch: pytest.MonkeyPatch):
    """Keep request authority and rate-limit state independent per test."""
    access_control._agent_roles.clear()
    monkeypatch.setattr(access_control, "_loaded", True)
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    mcp_server._INGEST_RATE_BUCKETS.clear()
    yield
    mcp_server._INGEST_RATE_BUCKETS.clear()
    access_control._agent_roles.clear()


def _seed_lexical_claim(tmp_path, text: str) -> tuple[str, str, int]:
    workspace = tmp_path / "allowed"
    workspace.mkdir()
    db = str(tmp_path / "retrieval-quarantine.db")
    service = MemoryService(db, workspace_root=workspace)
    service.init_db()
    claim = service.ingest(
        text,
        [CitationInput(source="test://retrieval-quarantine")],
        scope="project:allowed",
        source_agent="seed",
    )
    return db, str(workspace), claim.id


class _RecordingQdrant:
    def __init__(self, calls: list[str]) -> None:
        calls.append("constructed")
        self._calls = calls

    def search(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        self._calls.append("searched")
        return []

    def sync_all(self, _store: Any) -> dict[str, int]:
        self._calls.append("synced")
        return {"synced": 0, "total": 0, "errors": 0}

    def close(self) -> None:
        self._calls.append("closed")


def _install_recording_qdrant(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
) -> None:
    monkeypatch.setattr(
        qdrant_backend,
        "QdrantBackend",
        lambda **_kwargs: _RecordingQdrant(calls),
    )


def test_local_trusted_explicit_qdrant_falls_back_to_authoritative_lexical(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, workspace, claim_id = _seed_lexical_claim(
        tmp_path,
        "quarantine lexical authority marker",
    )
    qdrant_calls: list[str] = []
    _install_recording_qdrant(monkeypatch, qdrant_calls)

    result = mcp_server.query_memory(
        query="quarantine lexical authority marker",
        db=db,
        workspace=workspace,
        retrieval_mode="qdrant",
        scope_allowlist="project:allowed",
    )

    assert qdrant_calls == []
    assert result["requested_retrieval_mode"] == "qdrant"
    assert result["retrieval_mode"] == "legacy"
    assert result["containment_reason"]
    assert {claim["id"] for claim in result["claims"]} == {claim_id}


def test_local_trusted_auto_classified_qdrant_falls_back_to_authoritative_lexical(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, workspace, claim_id = _seed_lexical_claim(
        tmp_path,
        "auto classified quarantine marker",
    )
    qdrant_calls: list[str] = []
    _install_recording_qdrant(monkeypatch, qdrant_calls)
    monkeypatch.setattr(query_classifier, "classify_query", lambda _query: "relational")
    monkeypatch.setattr(
        query_classifier,
        "recommended_retrieval_mode",
        lambda _query_type: "qdrant",
    )

    result = mcp_server.query_memory(
        query="auto classified quarantine marker",
        db=db,
        workspace=workspace,
        retrieval_mode="legacy",
        auto_classify=True,
        scope_allowlist="project:allowed",
    )

    assert qdrant_calls == []
    assert result["query_type"] == "relational"
    assert result["requested_retrieval_mode"] == "legacy"
    assert result["classified_retrieval_mode"] == "qdrant"
    assert result["retrieval_mode"] == "legacy"
    assert result["containment_reason"]
    assert {claim["id"] for claim in result["claims"]} == {claim_id}


def test_classify_query_reports_quarantined_effective_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(query_classifier, "classify_query", lambda _query: "relational")
    monkeypatch.setattr(
        query_classifier,
        "recommended_retrieval_mode",
        lambda _query_type: "qdrant",
    )

    result = mcp_server.classify_query("what depends on this?")

    assert result["query_type"] == "relational"
    assert result["recommended_mode"] == "qdrant"
    assert result["effective_mode"] == "legacy"
    assert result["containment_reason"]


@pytest.mark.parametrize(
    "semantic_args",
    [
        {"retrieval_mode": "qdrant"},
        {"retrieval_mode": "legacy", "auto_classify": True},
    ],
)
def test_team_mode_semantic_retrieval_remains_denied_before_body(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    semantic_args: dict[str, Any],
) -> None:
    workspace = tmp_path / "team-workspace"
    workspace.mkdir()
    access_control.set_role("mcp-reader", access_control.Role.READER)
    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "team")
    monkeypatch.setenv("MEMORYMASTER_MCP_PRINCIPAL", "mcp-reader")
    monkeypatch.setenv("MEMORYMASTER_MCP_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("MEMORYMASTER_MCP_WORKSPACE", str(workspace))
    monkeypatch.setenv("MEMORYMASTER_MCP_ALLOWED_SCOPES", "project:alpha,global")
    monkeypatch.setenv("MEMORYMASTER_MCP_DB", str(tmp_path / "team.db"))
    monkeypatch.setattr(
        mcp_server,
        "_service",
        lambda *_args, **_kwargs: pytest.fail("semantic denial reached the service"),
    )
    monkeypatch.setattr(
        mcp_server,
        "_qdrant_query",
        lambda *_args, **_kwargs: pytest.fail("semantic denial reached Qdrant"),
    )

    with pytest.raises(PermissionError, match="(?i)(semantic|team|disabled)"):
        mcp_server.query_memory(query="team semantic request", **semantic_args)


def test_cli_qdrant_search_is_denied_before_backend_construction(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    qdrant_calls: list[str] = []
    _install_recording_qdrant(monkeypatch, qdrant_calls)
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.invalid")

    return_code = main(
        ["--db", str(tmp_path / "cli.db"), "qdrant-search", "quarantined query"]
    )
    output = capsys.readouterr().out.lower()

    assert qdrant_calls == []
    assert return_code == 2
    assert "qdrant" in output


def test_cli_qdrant_search_returns_code_two(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    qdrant_calls: list[str] = []
    _install_recording_qdrant(monkeypatch, qdrant_calls)

    return_code = main(
        ["--db", str(tmp_path / "cli.db"), "qdrant-search", "quarantined query"]
    )
    output = capsys.readouterr().out.lower()

    assert return_code == 2
    assert "qdrant" in output


def test_cli_qdrant_search_json_error_is_one_valid_document(
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    return_code = main(
        ["--json", "--db", str(tmp_path / "cli.db"), "qdrant-search", "query"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert return_code == 2
    assert payload["ok"] is False
    assert "qdrant" in payload["error"].lower()


def test_cli_auto_classified_qdrant_uses_lexical_and_valid_json(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db, workspace, claim_id = _seed_lexical_claim(
        tmp_path,
        "cli auto classified quarantine marker",
    )
    monkeypatch.setattr(query_classifier, "classify_query", lambda _query: "relational")
    monkeypatch.setattr(
        query_classifier,
        "recommended_retrieval_mode",
        lambda _query_type: "qdrant",
    )

    return_code = main(
        [
            "--json",
            "--db",
            db,
            "--workspace",
            workspace,
            "query",
            "cli auto classified quarantine marker",
            "--auto-classify",
            "--include-candidates",
            "--scope-allowlist",
            "project:allowed",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert return_code == 0
    assert {row["claim"]["id"] for row in payload["data"]} == {claim_id}
    assert payload["meta"]["query_type"] == "relational"
    assert payload["meta"]["requested_retrieval_mode"] == "legacy"
    assert payload["meta"]["classified_retrieval_mode"] == "qdrant"
    assert payload["meta"]["retrieval_mode"] == "legacy"
    assert payload["meta"]["containment_reason"]


def test_cli_qdrant_sync_remains_available(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    qdrant_calls: list[str] = []
    _install_recording_qdrant(monkeypatch, qdrant_calls)

    return_code = main(["--db", str(tmp_path / "cli.db"), "qdrant-sync"])
    output = capsys.readouterr().out

    assert return_code == 0
    assert "Qdrant sync" in output
    assert qdrant_calls == ["constructed", "synced"]
