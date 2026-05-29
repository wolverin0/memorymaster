from __future__ import annotations

import pytest

from memorymaster.models import CitationInput
from memorymaster.security import redact_text, sanitize_claim_input


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


# --- sanitize_claim_input: subject/predicate are filtered at ingest ---------
# Regression for audit finding ingest-subject-skips-filter: subject/predicate
# are exposed MCP ingest params that reached the store unredacted. The ingest
# filter (last line of defense) must catch a secret in either field.

_SECRET = "ghp_fakefakefakefakefakefakefakefakefake"
_CITE = [CitationInput(source="s", locator="l")]


def test_sanitize_redacts_secret_in_subject() -> None:
    s = sanitize_claim_input(
        text="benign claim text", object_value=None, citations=_CITE,
        subject=f"token {_SECRET}", predicate="aspect",
    )
    assert s.is_sensitive
    assert "github_token" in s.findings
    assert _SECRET not in (s.subject or "")
    assert "[REDACTED:github_token]" in (s.subject or "")


def test_sanitize_redacts_secret_in_predicate() -> None:
    s = sanitize_claim_input(
        text="benign claim text", object_value=None, citations=_CITE,
        subject="entity", predicate=f"uses {_SECRET}",
    )
    assert s.is_sensitive
    assert _SECRET not in (s.predicate or "")
    assert "[REDACTED:github_token]" in (s.predicate or "")


def test_sanitize_clean_subject_predicate_pass_through() -> None:
    s = sanitize_claim_input(
        text="benign", object_value=None, citations=_CITE,
        subject="database", predicate="version",
    )
    assert not s.is_sensitive
    assert s.subject == "database"
    assert s.predicate == "version"
