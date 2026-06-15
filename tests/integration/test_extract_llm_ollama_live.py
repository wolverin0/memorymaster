"""Integration smoke test for Layer-2 LLM entity extraction with live Ollama.

This test calls a real Ollama instance (default: http://localhost:11434).
It is skipped unless MEMORYMASTER_TEST_OLLAMA_LIVE=1 is set.

Usage:
    MEMORYMASTER_TEST_OLLAMA_LIVE=1 pytest tests/integration/test_extract_llm_ollama_live.py -v
"""
import os
import pytest


@pytest.mark.skipif(
    not os.environ.get("MEMORYMASTER_TEST_OLLAMA_LIVE"),
    reason="set MEMORYMASTER_TEST_OLLAMA_LIVE=1 to run live Ollama smoke",
)
def test_extract_llm_ollama_live():
    """Live Ollama smoke test with Gemma model."""
    os.environ["MEMORYMASTER_ENTITY_LLM"] = "1"
    os.environ["MEMORYMASTER_LLM_PROVIDER"] = "ollama"
    os.environ["MEMORYMASTER_LLM_MODEL"] = "gemma4:e4b"

    from memorymaster.knowledge.entity_extractor import extract_llm

    # Sample text: Spanish + English mix with person names, library, and model
    text = "Ada Lovelace y Charles Babbage usaron FastAPI y gpt-4o-mini."

    result = extract_llm(text)

    # Assertions: expect at least person_name entities for the two names
    assert len(result) >= 2, (
        f"expected ≥2 entities, got {len(result)}: {result}"
    )

    # Check that at least one person_name was extracted
    person_entities = [e for e in result if e.kind == "person_name"]
    assert len(person_entities) >= 1, (
        f"expected ≥1 person_name, got {person_entities}"
    )

    # Verify surfaces are non-empty and reasonable
    for entity in result:
        assert entity.surface, f"empty surface for entity {entity}"
        assert entity.kind, f"empty kind for entity {entity}"
        assert entity.canonical_hint, f"empty canonical_hint for entity {entity}"

    print(f"[OK] Extracted {len(result)} entities:")
    for e in result:
        print(f"  - {e.surface!r} (kind={e.kind})")
