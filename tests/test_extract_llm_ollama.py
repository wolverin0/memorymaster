"""Unit tests for Layer-2 LLM entity extraction with Ollama/Gemma schema variants.

Tests mock the `call_llm` function to simulate different response formats,
including the Gemma variant that uses "entity" instead of "surface_form".
"""
import os
from unittest.mock import patch

from memorymaster.knowledge.entity_extractor import extract_llm


class TestExtractLlmOllamaSchemaVariants:
    """Test extract_llm parser robustness against schema variants."""

    @patch.dict(os.environ, {"MEMORYMASTER_ENTITY_LLM": "1"})
    @patch("memorymaster.core.llm_provider.call_llm")
    def test_standard_schema_surface_form(self, mock_call_llm):
        """Standard schema with 'surface_form' field."""
        mock_call_llm.return_value = (
            '['
            '{"kind":"person_name","surface_form":"Ada Lovelace","aliases":[]}'
            ']'
        )
        result = extract_llm("Ada Lovelace wrote about computing.")
        assert len(result) == 1
        assert result[0].surface == "Ada Lovelace"
        assert result[0].kind == "person_name"

    @patch.dict(os.environ, {"MEMORYMASTER_ENTITY_LLM": "1"})
    @patch("memorymaster.core.llm_provider.call_llm")
    def test_gemma_alt_schema_entity_field(self, mock_call_llm):
        """Gemma variant using 'entity' instead of 'surface_form'."""
        mock_call_llm.return_value = (
            '['
            '{"kind":"person_name","entity":"Ada Lovelace"},'
            '{"kind":"person_name","entity":"Charles Babbage"}'
            ']'
        )
        result = extract_llm("Ada Lovelace and Charles Babbage.")
        assert len(result) == 2
        assert result[0].surface == "Ada Lovelace"
        assert result[1].surface == "Charles Babbage"

    @patch.dict(os.environ, {"MEMORYMASTER_ENTITY_LLM": "1"})
    @patch("memorymaster.core.llm_provider.call_llm")
    def test_gemma_variant_mixed_fields(self, mock_call_llm):
        """Gemma variant with mixed or missing 'aliases'."""
        mock_call_llm.return_value = (
            '['
            '{"kind":"person_name","entity":"Ada Lovelace"},'
            '{"kind":"model_name","entity":"gpt-4o-mini","aliases":[]}'
            ']'
        )
        result = extract_llm("Ada Lovelace used gpt-4o-mini.")
        assert len(result) == 2
        assert result[0].surface == "Ada Lovelace"
        assert result[1].surface == "gpt-4o-mini"

    @patch.dict(os.environ, {"MEMORYMASTER_ENTITY_LLM": "1"})
    @patch("memorymaster.core.llm_provider.call_llm")
    def test_truncated_json_response_no_crash(self, mock_call_llm):
        """Truncated JSON response doesn't crash."""
        # Simulate Ollama cutting off mid-JSON
        mock_call_llm.return_value = '```json\n[{"kind":"person_name","entity":"Ada"'
        result = extract_llm("Ada Lovelace and Charles Babbage.")
        # Should return empty list, not crash
        assert result == []

    @patch.dict(os.environ, {"MEMORYMASTER_ENTITY_LLM": "1"})
    @patch("memorymaster.core.llm_provider.call_llm")
    def test_multiple_kinds_mixed_schema(self, mock_call_llm):
        """Multiple entity kinds with mixed schema."""
        mock_call_llm.return_value = (
            '['
            '{"kind":"person_name","surface_form":"Ada Lovelace","aliases":[]},'
            '{"kind":"person_name","entity":"Charles Babbage"},'
            '{"kind":"library_name","entity":"FastAPI","aliases":[]},'
            '{"kind":"model_name","surface_form":"gpt-4o-mini","aliases":[]}'
            ']'
        )
        result = extract_llm(
            "Ada Lovelace y Charles Babbage usaron FastAPI y gpt-4o-mini."
        )
        assert len(result) == 4
        assert result[0].kind == "person_name"
        assert result[1].kind == "person_name"
        assert result[2].kind == "library_name"
        assert result[3].kind == "model_name"

    @patch.dict(os.environ, {"MEMORYMASTER_ENTITY_LLM": "1"})
    @patch("memorymaster.core.llm_provider.call_llm")
    def test_invalid_kind_filtered_out(self, mock_call_llm):
        """Invalid kind is filtered out."""
        mock_call_llm.return_value = (
            '['
            '{"kind":"person_name","entity":"Ada Lovelace"},'
            '{"kind":"invalid_kind","entity":"something"}'
            ']'
        )
        result = extract_llm("Ada Lovelace and something.")
        assert len(result) == 1
        assert result[0].surface == "Ada Lovelace"

    @patch.dict(os.environ, {"MEMORYMASTER_ENTITY_LLM": "1"})
    @patch("memorymaster.core.llm_provider.call_llm")
    def test_empty_surface_filtered_out(self, mock_call_llm):
        """Empty surface is filtered out."""
        mock_call_llm.return_value = (
            '['
            '{"kind":"person_name","entity":"Ada Lovelace"},'
            '{"kind":"person_name","entity":""}'
            ']'
        )
        result = extract_llm("Ada Lovelace.")
        assert len(result) == 1
        assert result[0].surface == "Ada Lovelace"

    @patch.dict(os.environ, {"MEMORYMASTER_ENTITY_LLM": "1"})
    @patch("memorymaster.core.llm_provider.call_llm")
    def test_malformed_json_returns_empty(self, mock_call_llm):
        """Malformed JSON returns empty list."""
        mock_call_llm.return_value = "not valid json"
        result = extract_llm("Ada Lovelace.")
        assert result == []

    @patch.dict(os.environ, {"MEMORYMASTER_ENTITY_LLM": "1"})
    @patch("memorymaster.core.llm_provider.call_llm")
    def test_non_list_dict_response_wrapped(self, mock_call_llm):
        """Non-list dict JSON response gets wrapped in list by parse_json_response."""
        # This is acceptable: parse_json_response wraps dicts in a list
        mock_call_llm.return_value = '{"kind":"person_name","entity":"Ada"}'
        result = extract_llm("Ada.")
        assert len(result) == 1
        assert result[0].surface == "Ada"

    @patch.dict(os.environ, {"MEMORYMASTER_ENTITY_LLM": "0"})
    def test_llm_disabled_returns_empty(self):
        """LLM disabled via env var returns empty."""
        result = extract_llm("Ada Lovelace.")
        assert result == []
