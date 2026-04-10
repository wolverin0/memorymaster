"""Tests for secret redaction patterns in security.py."""

from __future__ import annotations


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


# ---------------------------------------------------------------------------
# New patterns added 2026-04-10 (closing security audit gaps)
# ---------------------------------------------------------------------------

class TestAnthropicKeyPattern:
    def test_sk_ant_key(self):
        token = "sk-ant-api03-" + "A" * 40
        result, findings = _redact(f"Authorization: {token}")
        assert "anthropic_key" in findings or "openai_key" in findings
        assert token not in result


class TestGoogleApiKeyPattern:
    def test_google_api_key(self):
        token = "AIza" + "S" * 35  # 39 chars total (real Google key length)
        result, findings = _redact(f"api key is {token}")
        assert "google_api_key" in findings
        assert token not in result

    def test_google_no_false_positive_short(self):
        _, findings = _redact("AIzaTOO_SHORT")
        assert "google_api_key" not in findings


class TestAwsStsKeyPattern:
    def test_asia_key(self):
        token = "ASIAIOSFODNN7EXAMPLE"
        result, findings = _redact(f"AWS temp key: {token}")
        assert "aws_sts_key" in findings
        assert token not in result


class TestSlackTokenPattern:
    def test_slack_bot_token(self):
        token = "xoxb-12345-abcdefghij123"
        _, findings = _redact(f"slack webhook: {token}")
        assert "slack_token" in findings

    def test_slack_user_token(self):
        token = "xoxp-9876-zyxwvutsrq987"
        _, findings = _redact(token)
        assert "slack_token" in findings


class TestTelegramBotTokenPattern:
    def test_telegram_bot_token(self):
        token = "1234567890:AAEhBP0av28fakeTokenExample_1234567890"
        _, findings = _redact(f"TELEGRAM_BOT_TOKEN={token}")
        assert "telegram_bot_token" in findings


class TestExtendedGitHubPrefixes:
    def test_ghu_user_token(self):
        token = "ghu_" + "A" * 36
        _, findings = _redact(token)
        assert "github_token" in findings

    def test_ghs_server_token(self):
        token = "ghs_" + "1" * 36
        _, findings = _redact(token)
        assert "github_token" in findings

    def test_ghr_refresh_token(self):
        token = "ghr_" + "B" * 36
        _, findings = _redact(token)
        assert "github_token" in findings


class TestPrivateIPv4NotRedactedAtIngest:
    """Private IPs are intentionally NOT filtered at ingest time — they appear
    in legitimate infrastructure claims. Filtering happens at export time in
    dream_bridge._DREAM_EXTRA_PATTERNS."""

    def test_private_ip_not_redacted_by_canonical_filter(self):
        """Claims containing private IPs should pass through sanitize_claim_input."""
        for ip in ["10.0.0.5", "192.168.1.100", "172.16.0.1", "172.31.255.254"]:
            _, findings = _redact(f"server at {ip}")
            assert "private_ipv4" not in findings, f"private IP {ip} was incorrectly redacted at ingest time"


class TestDbUrlPasswordPattern:
    def test_postgres_url(self):
        url = "postgres://user:secretpass@localhost/mydb"
        result, findings = _redact(f"DATABASE_URL={url}")
        assert "db_url_password" in findings
        assert "secretpass" not in result

    def test_mongodb_srv_url(self):
        url = "mongodb+srv://admin:pwd123@cluster0.mongodb.net/db"
        _, findings = _redact(url)
        assert "db_url_password" in findings

    def test_redis_url(self):
        url = "redis://:topsecret@host:6379/0"
        _, findings = _redact(url)
        assert "db_url_password" in findings

    def test_mysql_url(self):
        url = "mysql://root:rootpass@db.example.com:3306/app"
        _, findings = _redact(url)
        assert "db_url_password" in findings


class TestRedactTextPublicApi:
    """Ensure the public redact_text re-export matches internal _redact."""

    def test_public_api_matches_internal(self):
        from memorymaster.security import redact_text, _redact
        text = "sk-1234567890abcdefghij"
        pub_result, pub_findings = redact_text(text)
        priv_result, priv_findings = _redact(text)
        assert pub_result == priv_result
        assert pub_findings == priv_findings
