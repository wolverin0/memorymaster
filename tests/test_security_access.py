"""Tests for memorymaster.security — access control, bypass, encryption."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from memorymaster.models import CitationInput
from memorymaster.security import (
    _as_bool,
    _encrypt_payload,
    _get_fernet,
    _sensitive_bypass_from_config,
    is_sensitive_bypass_enabled,
    is_sensitive_claim,
    resolve_allow_sensitive_access,
    sanitize_claim_input,
)


class TestAsBool:
    def test_bool_passthrough(self):
        assert _as_bool(True, field="x") is True
        assert _as_bool(False, field="x") is False

    def test_int_coercion(self):
        assert _as_bool(1, field="x") is True
        assert _as_bool(0, field="x") is False

    def test_float_coercion(self):
        assert _as_bool(1.0, field="x") is True
        assert _as_bool(0.0, field="x") is False

    def test_truthy_strings(self):
        for val in ("1", "true", "True", "YES", "on", "y"):
            assert _as_bool(val, field="x") is True

    def test_falsy_strings(self):
        for val in ("0", "false", "False", "NO", "off", "n"):
            assert _as_bool(val, field="x") is False

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError, match="boolean-like"):
            _as_bool("maybe", field="test_field")

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError):
            _as_bool([], field="x")


class TestSensitiveBypassFromConfig:
    def test_none_config(self):
        assert _sensitive_bypass_from_config(None) is None

    def test_empty_config(self):
        assert _sensitive_bypass_from_config({}) is None

    def test_top_level_key(self):
        assert _sensitive_bypass_from_config({"allow_sensitive_bypass": True}) is True
        assert _sensitive_bypass_from_config({"allow_sensitive_bypass": False}) is False

    def test_nested_security_key(self):
        config = {"security": {"allow_sensitive_access": "yes"}}
        assert _sensitive_bypass_from_config(config) is True

    def test_sensitive_bypass_enabled(self):
        config = {"sensitive_bypass_enabled": "1"}
        assert _sensitive_bypass_from_config(config) is True


class TestIsSensitiveBypassEnabled:
    def test_config_takes_precedence(self):
        with patch.dict(os.environ, {"MEMORYMASTER_ALLOW_SENSITIVE_BYPASS": "1"}):
            # Config says no, should be False
            assert is_sensitive_bypass_enabled({"allow_sensitive_bypass": False}) is False

    def test_env_var_fallback(self):
        with patch.dict(os.environ, {"MEMORYMASTER_ALLOW_SENSITIVE_BYPASS": "true"}):
            assert is_sensitive_bypass_enabled(None) is True

    def test_default_disabled(self):
        with patch.dict(os.environ, {}, clear=False):
            # Remove the env var if it exists
            os.environ.pop("MEMORYMASTER_ALLOW_SENSITIVE_BYPASS", None)
            assert is_sensitive_bypass_enabled(None) is False


class TestResolveAllowSensitiveAccess:
    def test_not_requested_returns_false(self):
        assert resolve_allow_sensitive_access(allow_sensitive=False, context="test") is False

    def test_allowed_with_bypass(self):
        with patch("memorymaster.security.is_sensitive_bypass_enabled", return_value=True):
            assert resolve_allow_sensitive_access(allow_sensitive=True, context="test") is True

    def test_denied_raises(self):
        with patch("memorymaster.security.is_sensitive_bypass_enabled", return_value=False):
            with pytest.raises(PermissionError, match="allow_sensitive access denied"):
                resolve_allow_sensitive_access(allow_sensitive=True, context="test")

    def test_filter_mode_returns_false(self):
        with patch("memorymaster.security.is_sensitive_bypass_enabled", return_value=False):
            result = resolve_allow_sensitive_access(
                allow_sensitive=True, context="test", deny_mode="filter"
            )
            assert result is False

    def test_invalid_deny_mode_raises(self):
        with patch("memorymaster.security.is_sensitive_bypass_enabled", return_value=False):
            with pytest.raises(ValueError, match="deny_mode"):
                resolve_allow_sensitive_access(
                    allow_sensitive=True, context="test", deny_mode="invalid"
                )


class TestGetFernet:
    def test_no_key_returns_none(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEMORYMASTER_ENCRYPTION_KEY", None)
            assert _get_fernet() is None

    def test_with_key(self):
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            pytest.skip("cryptography not installed")

        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"MEMORYMASTER_ENCRYPTION_KEY": key}):
            f = _get_fernet()
            assert f is not None


class TestEncryptPayload:
    def test_no_key_returns_none(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEMORYMASTER_ENCRYPTION_KEY", None)
            assert _encrypt_payload({"foo": "bar"}) is None

    def test_with_key_returns_string(self):
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            pytest.skip("cryptography not installed")

        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"MEMORYMASTER_ENCRYPTION_KEY": key}):
            result = _encrypt_payload({"text": "secret data"})
            assert result is not None
            assert isinstance(result, str)
            assert len(result) > 0


class TestSanitizeClaimInput:
    def test_clean_input(self):
        result = sanitize_claim_input(
            text="Python is great",
            object_value=None,
            citations=[],
        )
        assert result.text == "Python is great"
        assert result.is_sensitive is False
        assert result.findings == []
        assert result.encrypted_payload is None

    def test_sensitive_input_redacted(self):
        result = sanitize_claim_input(
            text="My key is sk-1234567890abcdef1234",
            object_value=None,
            citations=[],
        )
        assert "[REDACTED:" in result.text
        assert result.is_sensitive is True
        assert len(result.findings) > 0

    def test_object_value_redacted(self):
        result = sanitize_claim_input(
            text="safe text",
            object_value="password=secret123",
            citations=[],
        )
        assert result.is_sensitive is True

    def test_citation_excerpt_redacted(self):
        cite = CitationInput(source="file.py", locator="line:5", excerpt="token=sk-abcdefghijklmn")
        result = sanitize_claim_input(
            text="safe",
            object_value=None,
            citations=[cite],
        )
        assert result.is_sensitive is True


class TestIsSensitiveClaim:
    def test_redacted_claim_is_sensitive(self):
        from memorymaster.models import Claim
        claim = Claim(
            id=1, text="[REDACTED:openai_key]", idempotency_key=None,
            normalized_text=None, claim_type=None, subject=None, predicate=None,
            object_value=None, scope="project", volatility="medium",
            status="confirmed", confidence=0.5, pinned=False,
            supersedes_claim_id=None, replaced_by_claim_id=None,
            created_at="", updated_at="", last_validated_at=None, archived_at=None,
        )
        assert is_sensitive_claim(claim) is True

    def test_clean_claim_not_sensitive(self):
        from memorymaster.models import Claim
        claim = Claim(
            id=1, text="Python is great", idempotency_key=None,
            normalized_text=None, claim_type=None, subject=None, predicate=None,
            object_value=None, scope="project", volatility="medium",
            status="confirmed", confidence=0.5, pinned=False,
            supersedes_claim_id=None, replaced_by_claim_id=None,
            created_at="", updated_at="", last_validated_at=None, archived_at=None,
        )
        assert is_sensitive_claim(claim) is False
