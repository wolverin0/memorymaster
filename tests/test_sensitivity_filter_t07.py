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
