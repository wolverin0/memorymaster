"""Tests for role-based access control."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from memorymaster.access_control import (
    DEFAULT_ROLE,
    Role,
    ROLE_PERMISSIONS,
    check_permission,
    get_role,
    require_permission,
)


class TestRole:
    """Test Role enum."""

    def test_role_values(self):
        """Role enum has correct values."""
        assert Role.ADMIN.value == "admin"
        assert Role.WRITER.value == "writer"
        assert Role.READER.value == "reader"

    def test_role_members(self):
        """All expected roles are defined."""
        assert hasattr(Role, "ADMIN")
        assert hasattr(Role, "WRITER")
        assert hasattr(Role, "READER")

    def test_role_string_conversion(self):
        """Roles can be compared with strings."""
        assert Role.ADMIN == "admin"
        assert Role.WRITER == "writer"
        assert Role.READER == "reader"


class TestRolePermissions:
    """Test ROLE_PERMISSIONS mapping."""

    def test_admin_permissions(self):
        """Admin role has full permissions."""
        admin_perms = ROLE_PERMISSIONS[Role.ADMIN]
        assert "ingest" in admin_perms
        assert "query" in admin_perms
        assert "delete" in admin_perms
        assert "configure" in admin_perms
        assert "export" in admin_perms
        assert "steward" in admin_perms
        assert "compact" in admin_perms

    def test_writer_permissions(self):
        """Writer role has ingest, query, export."""
        writer_perms = ROLE_PERMISSIONS[Role.WRITER]
        assert "ingest" in writer_perms
        assert "query" in writer_perms
        assert "export" in writer_perms
        assert "delete" not in writer_perms
        assert "configure" not in writer_perms

    def test_reader_permissions(self):
        """Reader role has query and export only."""
        reader_perms = ROLE_PERMISSIONS[Role.READER]
        assert "query" in reader_perms
        assert "export" in reader_perms
        assert "ingest" not in reader_perms
        assert "delete" not in reader_perms
        assert "configure" not in reader_perms


class TestDefaultRole:
    """Test DEFAULT_ROLE constant."""

    def test_default_is_writer(self):
        """DEFAULT_ROLE is writer."""
        assert DEFAULT_ROLE == Role.WRITER


class TestGetRole:
    """Test get_role function."""

    def test_get_role_unknown_agent(self):
        """Unknown agent returns DEFAULT_ROLE (writer)."""
        # Reset module state
        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        role = get_role("unknown-agent")
        assert role == Role.WRITER

    def test_get_role_none_agent(self):
        """None agent returns DEFAULT_ROLE."""
        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        role = get_role(None)
        assert role == Role.WRITER

    @patch.dict(os.environ, {"MEMORYMASTER_ROLE_DASHBOARD": "reader"})
    def test_get_role_from_environment(self):
        """get_role reads from environment variables."""
        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        role = get_role("dashboard")
        assert role == Role.READER

    @patch.dict(os.environ, {"MEMORYMASTER_ROLE_AUDIT_AGENT": "admin"})
    def test_get_role_environment_underscore_conversion(self):
        """Environment variables convert underscores to hyphens."""
        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        role = get_role("audit-agent")
        assert role == Role.ADMIN

    def test_get_role_case_insensitive(self):
        """Agent IDs are case-insensitive."""
        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        with patch.dict(os.environ, {"MEMORYMASTER_ROLE_MY_AGENT": "admin"}):
            role1 = get_role("my-agent")
            role2 = get_role("MY-AGENT")
            assert role1 == role2 == Role.ADMIN

    def test_get_role_from_config_file(self, tmp_path):
        """get_role reads from config file."""
        config = {"agents": {"dashboard": "reader", "admin-tool": "admin"}}
        config_file = tmp_path / "roles.json"
        config_file.write_text(json.dumps(config))

        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        with patch.object(ac, "ROLES_CONFIG_PATH", str(config_file)):
            role = get_role("dashboard")
            assert role == Role.READER
            role = get_role("admin-tool")
            assert role == Role.ADMIN

    def test_get_role_config_priority_over_default(self, tmp_path):
        """Config file roles take precedence."""
        config = {"agents": {"special-agent": "admin"}}
        config_file = tmp_path / "roles.json"
        config_file.write_text(json.dumps(config))

        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        with patch.object(ac, "ROLES_CONFIG_PATH", str(config_file)):
            role = get_role("special-agent")
            assert role == Role.ADMIN


class TestCheckPermission:
    """Test check_permission function."""

    def test_check_permission_admin(self):
        """Admin has all permissions."""
        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        with patch.dict(os.environ, {"MEMORYMASTER_ROLE_ADMIN_TOOL": "admin"}):
            assert check_permission("admin-tool", "ingest")
            assert check_permission("admin-tool", "delete")
            assert check_permission("admin-tool", "configure")

    def test_check_permission_writer_ingest(self):
        """Writer can ingest."""
        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        with patch.dict(os.environ, {"MEMORYMASTER_ROLE_BOT": "writer"}):
            assert check_permission("bot", "ingest")
            assert check_permission("bot", "query")

    def test_check_permission_writer_no_delete(self):
        """Writer cannot delete."""
        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        with patch.dict(os.environ, {"MEMORYMASTER_ROLE_BOT": "writer"}):
            assert not check_permission("bot", "delete")
            assert not check_permission("bot", "configure")

    def test_check_permission_reader(self):
        """Reader can only query and export."""
        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        with patch.dict(os.environ, {"MEMORYMASTER_ROLE_DASHBOARD": "reader"}):
            assert check_permission("dashboard", "query")
            assert check_permission("dashboard", "export")
            assert not check_permission("dashboard", "ingest")
            assert not check_permission("dashboard", "delete")

    def test_check_permission_unknown_agent(self):
        """Unknown agents are writers by default."""
        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        assert check_permission("new-agent", "ingest")
        assert check_permission("new-agent", "query")
        assert not check_permission("new-agent", "delete")

    def test_check_permission_none_agent(self):
        """None agent uses default role."""
        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        assert check_permission(None, "ingest")
        assert not check_permission(None, "delete")


class TestRequirePermission:
    """Test require_permission function."""

    def test_require_permission_allowed(self):
        """require_permission succeeds when allowed."""
        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        with patch.dict(os.environ, {"MEMORYMASTER_ROLE_BOT": "writer"}):
            # Should not raise
            require_permission("bot", "ingest")
            require_permission("bot", "query")

    def test_require_permission_denied_raises(self):
        """require_permission raises PermissionError when denied."""
        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        with patch.dict(os.environ, {"MEMORYMASTER_ROLE_DASHBOARD": "reader"}):
            with pytest.raises(PermissionError) as exc_info:
                require_permission("dashboard", "ingest")
            assert "dashboard" in str(exc_info.value)
            assert "ingest" in str(exc_info.value)
            assert "reader" in str(exc_info.value)

    def test_require_permission_error_message(self):
        """PermissionError has informative message."""
        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        with patch.dict(os.environ, {"MEMORYMASTER_ROLE_DASHBOARD": "reader"}):
            try:
                require_permission("dashboard", "delete")
                pytest.fail("Should have raised PermissionError")
            except PermissionError as e:
                error_msg = str(e)
                assert "dashboard" in error_msg
                assert "reader" in error_msg
                assert "delete" in error_msg

    def test_require_permission_none_agent(self):
        """require_permission works with None agent."""
        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        # Should not raise (None uses default writer role)
        require_permission(None, "ingest")

        # Should raise for writer-forbidden action
        with pytest.raises(PermissionError):
            require_permission(None, "delete")


class TestPermissionWorkflow:
    """Integration tests for access control workflows."""

    def test_workflow_admin_full_access(self):
        """Admin workflow: full access to all operations."""
        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        with patch.dict(os.environ, {"MEMORYMASTER_ROLE_ADMIN": "admin"}):
            # All operations allowed
            require_permission("admin", "ingest")
            require_permission("admin", "query")
            require_permission("admin", "delete")
            require_permission("admin", "configure")

    def test_workflow_writer_ingestion(self):
        """Writer workflow: can ingest and query."""
        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        with patch.dict(os.environ, {"MEMORYMASTER_ROLE_BOT": "writer"}):
            require_permission("bot", "ingest")
            require_permission("bot", "query")
            with pytest.raises(PermissionError):
                require_permission("bot", "delete")

    def test_workflow_reader_readonly(self):
        """Reader workflow: query-only access."""
        import memorymaster.access_control as ac
        ac._loaded = False
        ac._agent_roles.clear()

        with patch.dict(os.environ, {"MEMORYMASTER_ROLE_DASHBOARD": "reader"}):
            require_permission("dashboard", "query")
            require_permission("dashboard", "export")
            with pytest.raises(PermissionError):
                require_permission("dashboard", "ingest")
            with pytest.raises(PermissionError):
                require_permission("dashboard", "delete")
