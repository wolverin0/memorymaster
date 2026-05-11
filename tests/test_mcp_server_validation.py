from __future__ import annotations

from memorymaster.mcp_server import (
    IngestClaimInput,
    _sensitive_input_error,
    _validate_tool_input,
)


def test_overlong_text_rejected() -> None:
    result = _validate_tool_input(IngestClaimInput, {"text": "x" * 10_001})

    assert isinstance(result, dict)
    assert result["code"] == "INPUT_TOO_LONG"
    assert result["field"] == "text"


def test_missing_required_field_returns_missing_field() -> None:
    result = _validate_tool_input(IngestClaimInput, {})

    assert isinstance(result, dict)
    assert result["code"] == "MISSING_FIELD"
    assert result["field"] == "text"


def test_jwt_like_string_blocked_by_sensitivity_filter() -> None:
    token = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )

    result = _sensitive_input_error(f"token={token}")

    assert result is not None
    assert result["code"] == "SENSITIVE_INPUT"
    assert result["field"] == "text"


def test_raw_dict_where_basemodel_expected_returns_clean_error() -> None:
    result = _validate_tool_input(
        IngestClaimInput,
        {"text": "valid claim"},
        allow_raw_dict=False,
    )

    assert isinstance(result, dict)
    assert result["code"] == "INVALID_INPUT"
    assert result["field"] == "request"
