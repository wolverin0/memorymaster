"""Tests for v3.19.0-H4 MCP db/workspace path allowlist.

Two layers:
- Unit tests on memorymaster.mcp_path_policy validators.
- Integration tests that exercise mcp_server._resolve_db / _resolve_workspace
  (the single chokepoint that all MCP tools share) to prove the policy
  applies uniformly without needing to touch every tool.
"""
from __future__ import annotations

import logging
import os
from typing import Iterator

import pytest

from memorymaster import mcp_path_policy
from memorymaster.mcp_path_policy import (
    ENV_ADMIN_MODE,
    ENV_DB_ALLOWLIST,
    ENV_WORKSPACE_ALLOWLIST,
    MCPPathPolicyError,
    admin_mode,
    validate_db_path,
    validate_workspace_path,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch) -> Iterator[None]:
    for var in (ENV_DB_ALLOWLIST, ENV_WORKSPACE_ALLOWLIST, ENV_ADMIN_MODE):
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------------------
# Unit: validate_db_path
# ---------------------------------------------------------------------------


def test_db_no_allowlist_is_no_op():
    """Back-compat: when env unset, any path passes through."""
    validate_db_path("/tmp/random-path.db")
    validate_db_path("anything.db")


def test_db_allowlist_exact_path_allowed(tmp_path, monkeypatch):
    db = tmp_path / "allowed.db"
    db.touch()
    monkeypatch.setenv(ENV_DB_ALLOWLIST, str(db))
    validate_db_path(str(db))  # no raise


def test_db_allowlist_other_path_denied(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed.db"
    allowed.touch()
    monkeypatch.setenv(ENV_DB_ALLOWLIST, str(allowed))
    forbidden = tmp_path / "forbidden.db"
    with pytest.raises(MCPPathPolicyError) as exc:
        validate_db_path(str(forbidden))
    assert "is not in" in str(exc.value)
    assert ENV_ADMIN_MODE in str(exc.value)


def test_db_allowlist_glob_pattern_match(tmp_path, monkeypatch):
    """fnmatch glob patterns let operators allow whole directory trees."""
    monkeypatch.setenv(ENV_DB_ALLOWLIST, str(tmp_path / "*.db"))
    target = tmp_path / "something.db"
    target.touch()
    validate_db_path(str(target))  # no raise


def test_db_allowlist_multiple_entries(tmp_path, monkeypatch):
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    a.touch()
    b.touch()
    monkeypatch.setenv(ENV_DB_ALLOWLIST, f"{a},{b}")
    validate_db_path(str(a))
    validate_db_path(str(b))
    with pytest.raises(MCPPathPolicyError):
        validate_db_path(str(tmp_path / "c.db"))


def test_db_admin_mode_bypasses_allowlist(tmp_path, monkeypatch, caplog):
    allowed = tmp_path / "allowed.db"
    allowed.touch()
    monkeypatch.setenv(ENV_DB_ALLOWLIST, str(allowed))
    monkeypatch.setenv(ENV_ADMIN_MODE, "1")
    with caplog.at_level(logging.WARNING, logger="memorymaster.mcp_path_policy"):
        validate_db_path("/anywhere/at/all.db")  # no raise
    assert any("admin_mode bypass" in rec.message for rec in caplog.records)


def test_db_admin_mode_recognizes_multiple_truthy_values(monkeypatch):
    for truthy in ("1", "true", "yes", "on", "TRUE", "Yes"):
        monkeypatch.setenv(ENV_ADMIN_MODE, truthy)
        assert admin_mode() is True
    for falsy in ("0", "false", "no", "", "off"):
        monkeypatch.setenv(ENV_ADMIN_MODE, falsy)
        assert admin_mode() is False


# ---------------------------------------------------------------------------
# Unit: validate_workspace_path
# ---------------------------------------------------------------------------


def test_workspace_no_allowlist_is_no_op():
    validate_workspace_path("/tmp/random-workspace")


def test_workspace_allowlist_exact_match_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_WORKSPACE_ALLOWLIST, str(tmp_path))
    validate_workspace_path(str(tmp_path))


def test_workspace_allowlist_other_path_denied(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_WORKSPACE_ALLOWLIST, str(tmp_path / "allowed"))
    with pytest.raises(MCPPathPolicyError):
        validate_workspace_path(str(tmp_path / "other"))


def test_workspace_admin_mode_bypass(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_WORKSPACE_ALLOWLIST, str(tmp_path / "narrow"))
    monkeypatch.setenv(ENV_ADMIN_MODE, "1")
    validate_workspace_path("/anywhere/else")  # no raise


# ---------------------------------------------------------------------------
# Integration: mcp_server._resolve_db / _resolve_workspace
# (proves the policy applies to every MCP tool via the single chokepoint)
# ---------------------------------------------------------------------------


def test_mcp_resolve_db_enforces_allowlist(tmp_path, monkeypatch):
    """A tool calling _resolve_db with a non-allowlisted path should raise.
    This covers every MCP tool that takes a `db` arg — no per-tool edits needed."""
    from memorymaster import mcp_server

    allowed_db = tmp_path / "allowed.db"
    allowed_db.touch()
    monkeypatch.setenv(ENV_DB_ALLOWLIST, str(allowed_db))

    # Allowed: returns the resolved path
    assert mcp_server._resolve_db(str(allowed_db)) == str(allowed_db)

    # Denied: raises MCPPathPolicyError
    with pytest.raises(MCPPathPolicyError):
        mcp_server._resolve_db(str(tmp_path / "evil.db"))


def test_mcp_resolve_workspace_enforces_allowlist(tmp_path, monkeypatch):
    from memorymaster import mcp_server

    allowed = tmp_path / "ws_ok"
    allowed.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ALLOWLIST, str(allowed))

    assert mcp_server._resolve_workspace(str(allowed)) == str(allowed)

    with pytest.raises(MCPPathPolicyError):
        mcp_server._resolve_workspace(str(tmp_path / "ws_evil"))


def test_mcp_resolve_db_default_path_passes_without_allowlist(monkeypatch):
    """Sanity: with no allowlist env set, the default DB resolves cleanly
    (back-compat: pre-v3.19 behaviour unchanged)."""
    from memorymaster import mcp_server

    # No allowlist set
    result = mcp_server._resolve_db("")
    assert isinstance(result, str)


def test_mcp_admin_mode_bypass_through_chokepoint(tmp_path, monkeypatch):
    """Admin mode lets a caller supply a non-allowlisted path through the
    same chokepoint that normally enforces."""
    from memorymaster import mcp_server

    allowed = tmp_path / "narrow.db"
    allowed.touch()
    monkeypatch.setenv(ENV_DB_ALLOWLIST, str(allowed))
    monkeypatch.setenv(ENV_ADMIN_MODE, "1")

    # Without admin mode this would raise; with admin mode it passes.
    result = mcp_server._resolve_db(str(tmp_path / "anywhere.db"))
    assert result == str(tmp_path / "anywhere.db")


def test_mcp_policy_error_log_includes_actor(tmp_path, monkeypatch, caplog):
    """Denial path logs a structured WARNING including the actor identifier."""
    from memorymaster import mcp_server

    allowed = tmp_path / "ok.db"
    allowed.touch()
    monkeypatch.setenv(ENV_DB_ALLOWLIST, str(allowed))

    with caplog.at_level(logging.WARNING, logger="memorymaster.mcp_path_policy"):
        with pytest.raises(MCPPathPolicyError):
            mcp_server._resolve_db(str(tmp_path / "evil.db"))

    assert any(
        "denied db=" in rec.message and "actor=mcp_caller" in rec.message
        for rec in caplog.records
    )
