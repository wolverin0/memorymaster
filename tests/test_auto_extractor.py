"""Tests for auto_extractor — LLM-based claim extraction from unstructured text.

Tests cover:
  - extract_claims_from_text: extraction with mocked LLM
  - _call_ollama: Ollama HTTP interaction
  - _normalise_claim: claim field normalization
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from memorymaster.auto_extractor import (
    _call_ollama,
    _normalise_claim,
    extract_claims_from_text,
)


class TestNormaliseClaim:
    """Test claim normalization."""

    def test_normalise_valid_claim(self) -> None:
        """Valid claim should pass through normalization."""
        raw = {
            "text": "The project uses React",
            "claim_type": "fact",
            "subject": "project",
            "predicate": "uses",
            "object_value": "React",
        }
        result = _normalise_claim(raw)

        assert result["text"] == "The project uses React"
        assert result["claim_type"] == "fact"
        assert result["subject"] == "project"
        assert result["predicate"] == "uses"
        assert result["object_value"] == "React"

    def test_normalise_missing_text(self) -> None:
        """Claim without text should return empty dict."""
        raw = {
            "claim_type": "fact",
            "subject": "test",
        }
        result = _normalise_claim(raw)

        assert result == {}

    def test_normalise_empty_text(self) -> None:
        """Claim with empty text should return empty dict."""
        raw = {
            "text": "",
            "claim_type": "fact",
        }
        result = _normalise_claim(raw)

        assert result == {}

    def test_normalise_missing_fields(self) -> None:
        """Claim missing optional fields should have them as None."""
        raw = {
            "text": "Simple fact",
        }
        result = _normalise_claim(raw)

        assert result["text"] == "Simple fact"
        assert result["claim_type"] == "fact"
        assert result["subject"] is None
        assert result["predicate"] is None
        assert result["object_value"] is None

    def test_normalise_whitespace_stripping(self) -> None:
        """Fields should have whitespace stripped."""
        raw = {
            "text": "  Fact with spaces  ",
            "claim_type": "  decision  ",
            "subject": "  test  ",
        }
        result = _normalise_claim(raw)

        assert result["text"] == "Fact with spaces"
        assert result["claim_type"] == "decision"
        assert result["subject"] == "test"

    def test_normalise_claim_type_default(self) -> None:
        """Missing claim_type should default to 'fact'."""
        raw = {
            "text": "Some fact",
        }
        result = _normalise_claim(raw)

        assert result["claim_type"] == "fact"

    def test_normalise_null_optional_fields(self) -> None:
        """None/empty optional fields should become None."""
        raw = {
            "text": "Some fact",
            "subject": None,
            "predicate": "",
            "object_value": "   ",
        }
        result = _normalise_claim(raw)

        assert result["subject"] is None
        assert result["predicate"] is None
        assert result["object_value"] is None


class TestCallOllama:
    """Test Ollama HTTP interaction."""

    @patch("memorymaster.auto_extractor.urllib.request.urlopen")
    def test_call_ollama_success(self, mock_urlopen: MagicMock) -> None:
        """Successful Ollama response should parse and return claims."""
        claims = [
            {
                "text": "React is used",
                "claim_type": "fact",
                "subject": "app",
                "predicate": "uses",
                "object_value": "React",
            }
        ]
        response_data = {
            "message": {"content": json.dumps(claims)},
        }

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(response_data).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = _call_ollama("test prompt", "http://localhost:11434", "test-model")

        assert len(result) == 1
        assert result[0]["text"] == "React is used"
        assert result[0]["claim_type"] == "fact"

    @patch("memorymaster.auto_extractor.urllib.request.urlopen")
    def test_call_ollama_with_markdown_fences(self, mock_urlopen: MagicMock) -> None:
        """Response with markdown fences should be stripped."""
        claims = [{"text": "Test claim", "claim_type": "fact"}]
        raw_response = f"```json\n{json.dumps(claims)}\n```"
        response_data = {
            "message": {"content": raw_response},
        }

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(response_data).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = _call_ollama("test prompt", "http://localhost:11434", "test-model")

        assert len(result) == 1
        assert result[0]["text"] == "Test claim"

    @patch("memorymaster.auto_extractor.urllib.request.urlopen")
    def test_call_ollama_returns_empty_on_url_error(self, mock_urlopen: MagicMock) -> None:
        """Ollama unreachable should return empty list."""
        import urllib.error

        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        result = _call_ollama("test prompt", "http://localhost:11434", "test-model")

        assert result == []

    @patch("memorymaster.auto_extractor.urllib.request.urlopen")
    def test_call_ollama_returns_empty_on_json_error(self, mock_urlopen: MagicMock) -> None:
        """Invalid JSON response should return empty list."""
        mock_response = MagicMock()
        mock_response.read.return_value = b"invalid json {"
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = _call_ollama("test prompt", "http://localhost:11434", "test-model")

        assert result == []

    @patch("memorymaster.auto_extractor.urllib.request.urlopen")
    def test_call_ollama_ignores_non_list_response(self, mock_urlopen: MagicMock) -> None:
        """Non-list JSON response should return empty list."""
        response_data = {
            "message": {"content": json.dumps({"error": "invalid"})},
        }

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(response_data).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = _call_ollama("test prompt", "http://localhost:11434", "test-model")

        assert result == []


class TestExtractClaimsFromText:
    """Test full extraction pipeline."""

    @patch("memorymaster.auto_extractor._call_ollama")
    def test_extract_claims_from_text_success(self, mock_ollama: MagicMock) -> None:
        """Successful extraction should return normalized claims."""
        mock_ollama.return_value = [
            {
                "text": "Using React 19",
                "claim_type": "fact",
                "subject": "app",
                "predicate": "uses",
                "object_value": "React 19",
            }
        ]

        result = extract_claims_from_text(
            "We are using React 19 for the frontend",
            source="conversation",
            scope="project",
        )

        assert len(result) == 1
        assert result[0]["text"] == "Using React 19"
        assert result[0]["source"] == "conversation"
        assert result[0]["scope"] == "project"

    @patch("memorymaster.auto_extractor._call_ollama")
    def test_extract_empty_text(self, mock_ollama: MagicMock) -> None:
        """Empty text should return empty list without calling Ollama."""
        result = extract_claims_from_text("", source="conversation")

        assert result == []
        mock_ollama.assert_not_called()

    @patch("memorymaster.auto_extractor._call_ollama")
    def test_extract_whitespace_only(self, mock_ollama: MagicMock) -> None:
        """Whitespace-only text should return empty list without calling Ollama."""
        result = extract_claims_from_text("   \n  \t  ", source="conversation")

        assert result == []
        mock_ollama.assert_not_called()

    @patch("memorymaster.auto_extractor._call_ollama")
    def test_extract_filters_invalid_claims(self, mock_ollama: MagicMock) -> None:
        """Invalid claims should be filtered out."""
        mock_ollama.return_value = [
            {"text": "Valid claim", "claim_type": "fact"},
            {"text": "", "claim_type": "fact"},  # Empty text
            {"claim_type": "fact"},  # No text field
        ]

        result = extract_claims_from_text("Test text", source="conversation")

        assert len(result) == 1
        assert result[0]["text"] == "Valid claim"

    @patch("memorymaster.auto_extractor._call_ollama")
    def test_extract_filters_non_dict_entries(self, mock_ollama: MagicMock) -> None:
        """Non-dict entries should be filtered out."""
        mock_ollama.return_value = [
            {"text": "Valid claim", "claim_type": "fact"},
            "not a dict",
            None,
            42,
        ]

        result = extract_claims_from_text("Test text", source="conversation")

        assert len(result) == 1
        assert result[0]["text"] == "Valid claim"

    @patch("memorymaster.auto_extractor._call_ollama")
    def test_extract_with_custom_model(self, mock_ollama: MagicMock) -> None:
        """Custom model should be passed to Ollama."""
        mock_ollama.return_value = []

        extract_claims_from_text(
            "Test",
            source="conversation",
            base_url="http://custom:11434",
            model="custom-model",
        )

        mock_ollama.assert_called_once()
        call_args = mock_ollama.call_args
        assert "http://custom:11434" in str(call_args)
        assert "custom-model" in str(call_args)

    @patch.dict("os.environ", {"OLLAMA_URL": "http://env-ollama:11434"})
    @patch("memorymaster.auto_extractor._call_ollama")
    def test_extract_respects_env_ollama_url(self, mock_ollama: MagicMock) -> None:
        """OLLAMA_URL env var should be respected."""
        mock_ollama.return_value = []

        extract_claims_from_text("Test", source="conversation")

        call_args = mock_ollama.call_args
        assert "http://env-ollama:11434" in str(call_args)

    @patch("memorymaster.auto_extractor._call_ollama")
    def test_extract_multiple_claims(self, mock_ollama: MagicMock) -> None:
        """Multiple claims should all be returned."""
        mock_ollama.return_value = [
            {"text": "Using React", "claim_type": "fact", "subject": "app"},
            {"text": "Using PostgreSQL", "claim_type": "fact", "subject": "db"},
            {"text": "Deploy to AWS", "claim_type": "decision", "subject": "infra"},
        ]

        result = extract_claims_from_text("Multi-fact text", source="conversation")

        assert len(result) == 3
        assert result[0]["subject"] == "app"
        assert result[1]["subject"] == "db"
        assert result[2]["claim_type"] == "decision"
