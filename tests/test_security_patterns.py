"""Tests for secret redaction patterns in security.py."""

from __future__ import annotations

import pytest

from memorymaster.security import _redact


class TestExistingPatterns:
    """Verify existing patterns still work after changes."""

    def test_openai_key(self):
        text = "key is sk-proj-abc123def456ghi"
        result, findings = _redact(text)
        assert "openai_key" in findings
        assert "[REDACTED:openai_key]" in result

    def test_aws_access_key(self):
        text = "aws key AKIAIOSFODNN7EXAMPLE"
        result, findings = _redact(text)
        assert "aws_access_key" in findings
        assert "[REDACTED:aws_access_key]" in result

    def test_private_key_pem(self):
        text = "-----BEGIN RSA PRIVATE KEY-----"
        result, findings = _redact(text)
        assert "private_key" in findings
        assert "[REDACTED:private_key]" in result

    def test_password_assignment(self):
        text = "password=hunter2"
        result, findings = _redact(text)
        assert "password_assignment" in findings
        assert "[REDACTED:password_assignment]" in result

    def test_token_assignment(self):
        text = "api_key=abcdef12345"
        result, findings = _redact(text)
        assert "token_assignment" in findings
        assert "[REDACTED:token_assignment]" in result


class TestJWTPattern:
    """JWT tokens: eyJ... base64 three-part format."""

    def test_typical_jwt(self):
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0"
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        text = f"Authorization header contained {jwt}"
        result, findings = _redact(text)
        assert "jwt_token" in findings
        assert jwt not in result
        assert "[REDACTED:jwt_token]" in result

    def test_jwt_no_false_positive_on_short_string(self):
        text = "eyJhb is not a full JWT"
        result, findings = _redact(text)
        assert "jwt_token" not in findings


class TestGitHubTokenPattern:
    """GitHub tokens: ghp_, gho_, github_pat_ prefixes."""

    def test_ghp_token(self):
        token = "ghp_ABCDEFghijklmnopqrstuvwxyz0123456789"
        text = f"GITHUB_TOKEN={token}"
        result, findings = _redact(text)
        assert "github_token" in findings
        assert token not in result

    def test_gho_token(self):
        token = "gho_ABCDEFghijklmnopqrstuvwxyz0123456789"
        text = f"token is {token}"
        result, findings = _redact(text)
        assert "github_token" in findings
        assert token not in result

    def test_github_pat(self):
        token = "github_pat_11ABCDEF0123456789abcdef0123456789abcdef0123456789abcdef01234567"
        text = f"use {token} for auth"
        result, findings = _redact(text)
        assert "github_token" in findings
        assert token not in result

    def test_no_false_positive_ghp_short(self):
        text = "ghp_short is not a real token"
        result, findings = _redact(text)
        assert "github_token" not in findings


class TestBearerTokenPattern:
    """OAuth Bearer tokens."""

    def test_bearer_token(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"
        result, findings = _redact(text)
        assert "bearer_token" in findings
        assert "Bearer eyJ" not in result

    def test_bearer_opaque_token(self):
        text = "Bearer ya29.a0AfH6SMBx-LONG_OPAQUE_TOKEN_1234"
        result, findings = _redact(text)
        assert "bearer_token" in findings

    def test_no_false_positive_bearer_short(self):
        text = "Bearer abc"
        result, findings = _redact(text)
        assert "bearer_token" not in findings


class TestOpenSSHPrivateKey:
    """OpenSSH private key header (covered by existing private_key pattern)."""

    def test_openssh_key(self):
        text = "-----BEGIN OPENSSH PRIVATE KEY-----"
        result, findings = _redact(text)
        assert "private_key" in findings
        assert "[REDACTED:private_key]" in result


class TestCleanTextNoRedaction:
    """Ensure clean text is not redacted."""

    def test_no_redaction(self):
        text = "This is a normal sentence with no secrets."
        result, findings = _redact(text)
        assert result == text
        assert findings == []
