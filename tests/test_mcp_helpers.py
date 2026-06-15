"""Tests for memorymaster.surfaces.mcp_server helper functions and tool wrappers."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from unittest.mock import patch

import pytest


def _port_bound(host: str = "127.0.0.1", port: int = 8765) -> bool:
    """True if *something* is already listening on (host, port).

    Used to skip ``test_open_dashboard_no_server`` when a real dashboard
    process is already running on the default port — otherwise the assertion
    ``reachable is False`` fails locally even though the test logic is fine.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False

from memorymaster.surfaces.mcp_server import (
    _claim_to_dict,
    _effective_ingest_scope,
    _effective_scope_allowlist,
    _empty_to_none,
    _parse_scope_allowlist,
    _parse_sources_json,
    _project_scope,
    _resolve_db,
    _resolve_workspace,
)


class TestResolveDb:
    def test_returns_explicit_db(self):
        assert _resolve_db("custom.db") == "custom.db"

    def test_returns_default_when_empty(self):
        with patch.dict(os.environ, {}, clear=False):
            with patch("memorymaster.surfaces.mcp_server._ENV_DEFAULT_DB", ""):
                assert _resolve_db("") == "memorymaster.db"

    def test_returns_env_default_when_no_explicit(self):
        with patch("memorymaster.surfaces.mcp_server._ENV_DEFAULT_DB", "env.db"):
            assert _resolve_db("memorymaster.db") == "env.db"

    def test_strips_whitespace(self):
        assert _resolve_db("  mydb.db  ") == "mydb.db"


class TestResolveWorkspace:
    def test_returns_explicit_workspace(self):
        assert _resolve_workspace("/some/path") == "/some/path"

    def test_returns_env_default(self):
        with patch("memorymaster.surfaces.mcp_server._ENV_DEFAULT_WORKSPACE", "/env/ws"):
            assert _resolve_workspace("") == "/env/ws"

    def test_returns_dot_when_no_override(self):
        with patch("memorymaster.surfaces.mcp_server._ENV_DEFAULT_WORKSPACE", ""):
            assert _resolve_workspace("") == "."


class TestEmptyToNone:
    def test_empty_returns_none(self):
        assert _empty_to_none("") is None
        assert _empty_to_none("   ") is None

    def test_non_empty_returns_stripped(self):
        assert _empty_to_none("  hello  ") == "hello"


class TestParseSourcesJson:
    def test_empty_string(self):
        assert _parse_sources_json("") == []
        assert _parse_sources_json("   ") == []

    def test_valid_sources(self):
        sources = json.dumps(["file.py|line:10|some excerpt", "readme.md"])
        result = _parse_sources_json(sources)
        assert len(result) == 2
        assert result[0].source == "file.py"
        assert result[0].locator == "line:10"
        assert result[0].excerpt == "some excerpt"
        assert result[1].source == "readme.md"
        assert result[1].locator is None
        assert result[1].excerpt is None

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="valid JSON"):
            _parse_sources_json("not json")

    def test_non_array_raises(self):
        with pytest.raises(ValueError, match="JSON array"):
            _parse_sources_json('"just a string"')

    def test_skips_non_string_items(self):
        result = _parse_sources_json('[123, "valid.py"]')
        assert len(result) == 1
        assert result[0].source == "valid.py"

    def test_skips_empty_source(self):
        result = _parse_sources_json('["||excerpt"]')
        assert len(result) == 0


class TestParseScopeAllowlist:
    def test_empty_returns_none(self):
        assert _parse_scope_allowlist("") is None
        assert _parse_scope_allowlist(None) is None

    def test_parses_comma_separated(self):
        result = _parse_scope_allowlist("project, global, custom")
        assert result == ["project", "global", "custom"]

    def test_strips_whitespace(self):
        result = _parse_scope_allowlist("  a , b  ")
        assert result == ["a", "b"]


class TestProjectScope:
    def test_returns_env_override(self):
        with patch("memorymaster.surfaces.mcp_server._ENV_DEFAULT_PROJECT_SCOPE", "override"):
            assert _project_scope(".") == "override"

    def test_generates_slug_from_workspace(self):
        with patch("memorymaster.surfaces.mcp_server._ENV_DEFAULT_PROJECT_SCOPE", ""):
            scope = _project_scope(".")
            assert scope.startswith("project:")
            assert ":" in scope  # has digest


class TestProjectScopeCanonicalization:
    """Regression tests for the 2026-04-22 scope-fragmentation audit.

    Live DB had: project:omniclaude vs project:_omniclaude (underscore variant),
    project:whatsappbot-final vs project:whatsapp-bot vs
    project:whatsappbot-prod - Copy - Copy (copy-artefact), and bare 'project'
    pollution for empty-workspace calls. Each case below must canonicalize to
    ONE scope.
    """

    def _scope(self, path):
        with patch("memorymaster.surfaces.mcp_server._ENV_DEFAULT_PROJECT_SCOPE", ""):
            return _project_scope(str(path))

    def test_underscore_prefix_folds(self, tmp_path):
        a = tmp_path / "omniclaude"
        a.mkdir()
        b = tmp_path / "_omniclaude"
        b.mkdir()
        assert self._scope(a) == "project:omniclaude"
        assert self._scope(b) == "project:omniclaude"

    def test_dash_prefix_folds(self, tmp_path):
        plain = tmp_path / "whatsapp"
        plain.mkdir()
        dashed = tmp_path / "-whatsapp"
        dashed.mkdir()
        assert self._scope(plain) == "project:whatsapp"
        assert self._scope(dashed) == "project:whatsapp"

    def test_channel_suffixes_fold(self, tmp_path):
        for name in ("whatsappbot", "whatsappbot-final", "whatsappbot-prod", "whatsappbot-staging"):
            (tmp_path / name).mkdir()
        assert self._scope(tmp_path / "whatsappbot") == "project:whatsappbot"
        assert self._scope(tmp_path / "whatsappbot-final") == "project:whatsappbot"
        assert self._scope(tmp_path / "whatsappbot-prod") == "project:whatsappbot"
        assert self._scope(tmp_path / "whatsappbot-staging") == "project:whatsappbot"

    def test_copy_artefact_stripped(self, tmp_path):
        plain = tmp_path / "whatsappbot"
        plain.mkdir()
        copied = tmp_path / "whatsappbot - Copy - Copy"
        copied.mkdir()
        assert self._scope(plain) == "project:whatsappbot"
        assert self._scope(copied) == "project:whatsappbot"

    def test_numeric_suffix_stripped(self, tmp_path):
        plain = tmp_path / "project-foo"
        plain.mkdir()
        numbered = tmp_path / "project-foo (1)"
        numbered.mkdir()
        assert self._scope(plain) == "project:project-foo"
        assert self._scope(numbered) == "project:project-foo"

    def test_empty_workspace_returns_user_not_project(self):
        """No workspace context anywhere → 'user' scope, not 'project'."""
        with patch("memorymaster.surfaces.mcp_server._ENV_DEFAULT_PROJECT_SCOPE", ""):
            with patch("memorymaster.surfaces.mcp_server._ENV_DEFAULT_WORKSPACE", ""):
                assert _project_scope("") == "user"
                assert _project_scope("   ") == "user"

    def test_disambiguate_env_still_adds_hash(self, tmp_path):
        """The v3.3.1 escape hatch must keep working for genuine collisions."""
        target = tmp_path / "myproj"
        target.mkdir()
        with patch("memorymaster.surfaces.mcp_server._ENV_DEFAULT_PROJECT_SCOPE", ""):
            with patch.dict(os.environ, {"MEMORYMASTER_SCOPE_DISAMBIGUATE": "1"}):
                scope = _project_scope(str(target))
                assert scope.startswith("project:myproj:")
                assert len(scope.split(":")[-1]) == 8  # 8-char sha1 digest


class TestEffectiveIngestScope:
    def test_empty_uses_project_scope(self):
        with patch("memorymaster.surfaces.mcp_server._project_scope", return_value="project:test:abc"):
            assert _effective_ingest_scope("", ".") == "project:test:abc"

    def test_project_literal_uses_project_scope(self):
        with patch("memorymaster.surfaces.mcp_server._project_scope", return_value="project:test:abc"):
            assert _effective_ingest_scope("project", ".") == "project:test:abc"

    def test_custom_scope_passthrough(self):
        assert _effective_ingest_scope("global", ".") == "global"


class TestEffectiveScopeAllowlist:
    def test_explicit_allowlist_wins(self):
        result = _effective_scope_allowlist("custom1,custom2", ".")
        assert result == ["custom1", "custom2"]

    def test_default_includes_project_and_global(self):
        with patch("memorymaster.surfaces.mcp_server._project_scope", return_value="project:ws:abc"):
            with patch("memorymaster.surfaces.mcp_server._ENV_QUERY_INCLUDE_LEGACY_PROJECT", True):
                result = _effective_scope_allowlist("", ".")
                assert "project:ws:abc" in result
                assert "global" in result
                assert "project" in result

    def test_no_legacy_project_when_disabled(self):
        with patch("memorymaster.surfaces.mcp_server._project_scope", return_value="project:ws:abc"):
            with patch("memorymaster.surfaces.mcp_server._ENV_QUERY_INCLUDE_LEGACY_PROJECT", False):
                result = _effective_scope_allowlist("", ".")
                assert "project" not in result


class TestClaimToDict:
    def test_converts_dataclass(self):
        from dataclasses import dataclass

        @dataclass
        class FakeClaim:
            id: int = 1
            text: str = "test"

        result = _claim_to_dict(FakeClaim())
        assert result == {"id": 1, "text": "test"}


class TestMcpToolsIntegration:
    """Test the actual MCP tool functions if FastMCP is available."""

    @pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        self.db_path = str(tmp_path / "test.db")
        self.workspace = str(tmp_path)

    def _init_service(self):
        from memorymaster.core.service import MemoryService
        svc = MemoryService(db_target=self.db_path, workspace_root=Path(self.workspace))
        svc.init_db()
        return svc

    def test_init_db_tool(self):
        try:
            from memorymaster.surfaces.mcp_server import init_db
        except ImportError:
            pytest.skip("MCP not installed")
        result = init_db(db=self.db_path, workspace=self.workspace)
        assert result["ok"] is True

    def test_ingest_and_query_tools(self):
        try:
            from memorymaster.surfaces.mcp_server import init_db, ingest_claim, query_memory
        except ImportError:
            pytest.skip("MCP not installed")

        init_db(db=self.db_path, workspace=self.workspace)
        result = ingest_claim(
            text="Python uses indentation for blocks",
            db=self.db_path,
            workspace=self.workspace,
            sources_json='["test.py"]',
        )
        assert result["ok"] is True
        assert result["claim"]["text"] == "Python uses indentation for blocks"

        query_result = query_memory(
            query="Python indentation",
            db=self.db_path,
            workspace=self.workspace,
        )
        assert query_result["ok"] is True
        assert query_result["rows"] >= 1

    def test_list_claims_tool(self):
        try:
            from memorymaster.surfaces.mcp_server import init_db, ingest_claim, list_claims
        except ImportError:
            pytest.skip("MCP not installed")

        init_db(db=self.db_path, workspace=self.workspace)
        ingest_claim(text="Test claim", db=self.db_path, workspace=self.workspace, sources_json='["test.py"]')
        result = list_claims(db=self.db_path, workspace=self.workspace)
        assert result["ok"] is True
        assert result["rows"] >= 1

    def test_pin_claim_tool(self):
        try:
            from memorymaster.surfaces.mcp_server import init_db, ingest_claim, pin_claim
        except ImportError:
            pytest.skip("MCP not installed")

        init_db(db=self.db_path, workspace=self.workspace)
        ingested = ingest_claim(text="Pin me", db=self.db_path, workspace=self.workspace, sources_json='["test.py"]')
        cid = ingested["claim"]["id"]
        result = pin_claim(claim_id=cid, db=self.db_path, workspace=self.workspace)
        assert result["ok"] is True
        assert result["claim"]["pinned"] is True

    def test_run_cycle_tool(self):
        try:
            from memorymaster.surfaces.mcp_server import init_db, run_cycle
        except ImportError:
            pytest.skip("MCP not installed")

        init_db(db=self.db_path, workspace=self.workspace)
        result = run_cycle(db=self.db_path, workspace=self.workspace)
        assert result["ok"] is True

    def test_compact_memory_tool(self):
        try:
            from memorymaster.surfaces.mcp_server import init_db, compact_memory
        except ImportError:
            pytest.skip("MCP not installed")

        init_db(db=self.db_path, workspace=self.workspace)
        result = compact_memory(db=self.db_path, workspace=self.workspace)
        assert result["ok"] is True

    def test_list_events_tool(self):
        try:
            from memorymaster.surfaces.mcp_server import init_db, list_events
        except ImportError:
            pytest.skip("MCP not installed")

        init_db(db=self.db_path, workspace=self.workspace)
        result = list_events(db=self.db_path, workspace=self.workspace)
        assert result["ok"] is True

    def test_query_for_context_tool(self):
        try:
            from memorymaster.surfaces.mcp_server import init_db, ingest_claim, query_for_context
        except ImportError:
            pytest.skip("MCP not installed")

        init_db(db=self.db_path, workspace=self.workspace)
        ingest_claim(text="Context test claim", db=self.db_path, workspace=self.workspace, sources_json='["test.py"]')
        result = query_for_context(
            query="context test",
            db=self.db_path,
            workspace=self.workspace,
        )
        assert result["ok"] is True
        assert "output" in result
        assert result["claims_considered"] >= 0

    def test_redact_claim_tool(self):
        try:
            from memorymaster.surfaces.mcp_server import init_db, ingest_claim, redact_claim_payload
        except ImportError:
            pytest.skip("MCP not installed")

        init_db(db=self.db_path, workspace=self.workspace)
        ingested = ingest_claim(text="Sensitive data", db=self.db_path, workspace=self.workspace, sources_json='["test.py"]')
        cid = ingested["claim"]["id"]
        result = redact_claim_payload(claim_id=cid, db=self.db_path, workspace=self.workspace)
        assert result["ok"] is True

    @pytest.mark.skipif(
        _port_bound(),
        reason="dashboard port 8765 is bound; this test requires no real server",
    )
    def test_open_dashboard_no_server(self):
        try:
            from memorymaster.surfaces.mcp_server import open_dashboard
        except ImportError:
            pytest.skip("MCP not installed")

        result = open_dashboard(check_health=True)
        assert result["ok"] is True
        assert "url" in result
        assert result["reachable"] is False  # no server running

    def test_open_dashboard_skip_health(self):
        try:
            from memorymaster.surfaces.mcp_server import open_dashboard
        except ImportError:
            pytest.skip("MCP not installed")

        result = open_dashboard(check_health=False)
        assert result["ok"] is True
        assert result["reachable"] is None
