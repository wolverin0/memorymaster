from __future__ import annotations

import pytest

from memorymaster.security import redact_text


def _assert_redacted(text: str, expected_finding: str, secret: str) -> None:
    result, findings = redact_text(text)
    assert expected_finding in findings
    assert secret not in result
    assert f"[REDACTED:{expected_finding}]" in result


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Bearer fakeBearerToken_1234567890", "fakeBearerToken_1234567890"),
        ("authorization bearer:fakeBearerToken_1234567890", "fakeBearerToken_1234567890"),
        ("BEARER_TOKEN=fakeBearerToken_1234567890", "fakeBearerToken_1234567890"),
    ],
)
def test_t07_bearer_prefix_variants_are_redacted(text: str, secret: str) -> None:
    _assert_redacted(text, "bearer_token", secret)


def test_t07_jwt_shape_is_redacted() -> None:
    token = (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiJmYWtlLXRlc3QifQ."
        "fakeJwtSignature_1234567890"
    )
    _assert_redacted(f"jwt={token}", "jwt_token", token)


@pytest.mark.parametrize(
    "uri",
    [
        "postgres://fake_user:fakePass123@db.example.invalid",
        "mysql://fake_user:fakePass123@db.example.invalid",
        "mongodb+srv://fake_user:fakePass123@cluster.example.invalid",
        "redis://fake_user:fakePass123@cache.example.invalid",
    ],
)
def test_t07_db_connection_strings_with_credentials_are_redacted(uri: str) -> None:
    _assert_redacted(f"DATABASE_URL={uri}", "db_url_password", uri)


def test_t07_aws_access_key_shape_is_redacted() -> None:
    token = "AKIAFAKEFAKEFAKE1234"
    _assert_redacted(f"AWS_ACCESS_KEY_ID={token}", "aws_access_key", token)


@pytest.mark.parametrize(
    "token",
    [
        "ghp_fakefakefakefakefakefakefakefakefake",
        "gho_fakefakefakefakefakefakefakefakefake",
        "ghu_fakefakefakefakefakefakefakefakefake",
    ],
)
def test_t07_github_pat_shapes_are_redacted(token: str) -> None:
    _assert_redacted(f"GITHUB_TOKEN={token}", "github_token", token)
