"""Tests for Layer-2 LLM entity extraction (`extract_llm`).

Unit tests mock `memorymaster.llm_provider.call_llm`. An integration test
(skipped by default) exercises the real provider when `GEMINI_API_KEY` or
`OPENAI_API_KEY` is present AND `MEMORYMASTER_ENTITY_LLM=1`.
"""
from __future__ import annotations

import json
import os
from unittest import mock

import pytest

from memorymaster.knowledge.entity_extractor import (
    Entity,
    LLM_KINDS,
    LLM_PROMPT_VERSION,
    extract_llm,
    extract_patterns,
    merge_entities,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_flag(monkeypatch):
    """Default every test to LLM-enabled; individual tests disable as needed."""
    monkeypatch.setenv("MEMORYMASTER_ENTITY_LLM", "1")


def _mock_call_llm(return_value: str):
    """Patch the call_llm used inside extract_llm."""
    return mock.patch(
        "memorymaster.llm_provider.call_llm", return_value=return_value
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_entities_of_permitted_kinds():
    payload = json.dumps(
        [
            {
                "kind": "person_name",
                "surface_form": "Ada Lovelace",
                "aliases": ["Lovelace"],
            },
            {
                "kind": "time_expression",
                "surface_form": "last Thursday",
                "aliases": [],
            },
            {
                "kind": "model_name",
                "surface_form": "gemini-3.1-flash-lite-preview",
                "aliases": ["gemini-3.1"],
            },
        ]
    )
    with _mock_call_llm(payload):
        result = extract_llm("Ada Lovelace shipped last Thursday on gemini-3.1-flash-lite-preview.")

    kinds = {e.kind for e in result}
    assert "person_name" in kinds
    assert "time_expression" in kinds
    assert "model_name" in kinds
    # Alias row included as a separate Entity
    assert any(e.canonical_hint == "Lovelace" for e in result)
    # Canonicalization: model_name lowercased
    assert any(e.canonical_hint == "gemini-3.1-flash-lite-preview" for e in result)


def test_env_var_unset_is_noop(monkeypatch):
    monkeypatch.delenv("MEMORYMASTER_ENTITY_LLM", raising=False)
    # Even if the LLM would have returned something, the gate kicks in first.
    with _mock_call_llm('[{"kind":"person_name","surface_form":"X"}]') as spy:
        result = extract_llm("some text")
    assert result == []
    spy.assert_not_called()


def test_env_var_falsy_is_noop(monkeypatch):
    for val in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("MEMORYMASTER_ENTITY_LLM", val)
        with _mock_call_llm("[]") as spy:
            assert extract_llm("text") == []
        spy.assert_not_called()


def test_empty_text_is_noop():
    with _mock_call_llm("[]") as spy:
        assert extract_llm("") == []
        assert extract_llm("   ") == []
    spy.assert_not_called()


# ---------------------------------------------------------------------------
# Defensive behavior
# ---------------------------------------------------------------------------


def test_malformed_json_returns_empty_list(caplog):
    with _mock_call_llm("not json at all"):
        result = extract_llm("Ada Lovelace did something")
    assert result == []


def test_llm_call_raises_returns_empty_list(caplog):
    with mock.patch(
        "memorymaster.llm_provider.call_llm",
        side_effect=RuntimeError("HTTP 500"),
    ):
        result = extract_llm("some text with Ada")
    assert result == []


def test_empty_response_returns_empty_list():
    with _mock_call_llm(""):
        assert extract_llm("some text") == []


def test_non_array_json_returns_empty_list():
    with _mock_call_llm('"hello world"'):
        assert extract_llm("some text") == []


def test_unknown_kind_filtered_out():
    payload = json.dumps(
        [
            {"kind": "file", "surface_form": "/etc/passwd"},          # forbidden
            {"kind": "commit", "surface_form": "deadbeef"},           # forbidden
            {"kind": "person_name", "surface_form": "Ada Lovelace"},  # allowed
        ]
    )
    with _mock_call_llm(payload):
        result = extract_llm("text")
    assert len(result) == 1
    assert result[0].kind == "person_name"


def test_empty_surface_is_dropped():
    payload = json.dumps(
        [
            {"kind": "person_name", "surface_form": "   "},
            {"kind": "concept", "surface_form": ""},
            {"kind": "library_name", "surface_form": "FastAPI"},
        ]
    )
    with _mock_call_llm(payload):
        result = extract_llm("text")
    assert [e.canonical_hint for e in result] == ["fastapi"]


def test_max_entities_cap_enforced():
    # 20 entities returned — we cap at 8.
    rows = [
        {"kind": "concept", "surface_form": f"concept_{i}"} for i in range(20)
    ]
    with _mock_call_llm(json.dumps(rows)):
        result = extract_llm("text")
    assert len(result) <= 8


def test_dedup_within_layer2():
    payload = json.dumps(
        [
            {"kind": "library_name", "surface_form": "FastAPI"},
            {"kind": "library_name", "surface_form": "fastapi"},  # dup after canon
            {"kind": "library_name", "surface_form": "FASTAPI"},  # dup after canon
        ]
    )
    with _mock_call_llm(payload):
        result = extract_llm("text")
    assert len(result) == 1
    assert result[0].canonical_hint == "fastapi"


# ---------------------------------------------------------------------------
# Merge with Layer-1 — dedup on (kind, canonical_hint)
# ---------------------------------------------------------------------------


def test_merge_dedups_across_layers():
    l1 = [
        Entity(surface="fastapi", kind="library_name", canonical_hint="fastapi"),
        Entity(surface="/etc/hosts", kind="file", canonical_hint="/etc/hosts"),
    ]
    l2 = [
        # Same (kind, canonical_hint) as l1[0] — should be deduped out.
        Entity(surface="FastAPI", kind="library_name", canonical_hint="fastapi"),
        Entity(surface="Ada Lovelace", kind="person_name", canonical_hint="Ada Lovelace"),
    ]
    merged = merge_entities(l1, l2)
    # 3 unique (kind, canonical) pairs: library_name/fastapi, file/..., person_name/...
    assert len(merged) == 3
    person = [e for e in merged if e.kind == "person_name"]
    assert person and person[0].canonical_hint == "Ada Lovelace"


def test_merge_preserves_layer1_surface_when_dup():
    l1 = [Entity(surface="fastapi", kind="library_name", canonical_hint="fastapi")]
    l2 = [Entity(surface="FastAPI", kind="library_name", canonical_hint="fastapi")]
    merged = merge_entities(l1, l2)
    assert len(merged) == 1
    # Layer-1 wins (first occurrence)
    assert merged[0].surface == "fastapi"


def test_merge_against_real_extract_patterns():
    """A real Layer-1 run + a mocked Layer-2 run composes cleanly."""
    text = "Deploy FastAPI to unms-server-01 on :8443 — Ada Lovelace confirmed."
    l1 = extract_patterns(text)
    payload = json.dumps(
        [
            {"kind": "person_name", "surface_form": "Ada Lovelace"},
            {"kind": "library_name", "surface_form": "FastAPI"},
        ]
    )
    with _mock_call_llm(payload):
        l2 = extract_llm(text)
    merged = merge_entities(l1, l2)
    kinds = {e.kind for e in merged}
    # Layer-1 kinds present
    assert "service" in kinds
    assert "port" in kinds
    # Layer-2 kinds present
    assert "person_name" in kinds
    assert "library_name" in kinds


# ---------------------------------------------------------------------------
# Canonical invariants
# ---------------------------------------------------------------------------


def test_all_permitted_kinds_covered_by_canonicalizer():
    """Every LLM_KINDS value must canonicalize to a non-empty string."""
    from memorymaster.knowledge.entity_extractor import _canonical_llm

    for kind in LLM_KINDS:
        out = _canonical_llm(kind, "  Some  Surface  ")
        assert out, f"empty canonical for {kind}"


def test_prompt_version_is_stable_string():
    assert isinstance(LLM_PROMPT_VERSION, str)
    assert LLM_PROMPT_VERSION


def test_provider_override_restores_env(monkeypatch):
    monkeypatch.setenv("MEMORYMASTER_LLM_PROVIDER", "google")
    with _mock_call_llm("[]"):
        extract_llm("text", provider="openai")
    assert os.environ["MEMORYMASTER_LLM_PROVIDER"] == "google"


def test_provider_override_restores_when_unset(monkeypatch):
    monkeypatch.delenv("MEMORYMASTER_LLM_PROVIDER", raising=False)
    with _mock_call_llm("[]"):
        extract_llm("text", provider="openai")
    assert "MEMORYMASTER_LLM_PROVIDER" not in os.environ


# ---------------------------------------------------------------------------
# Integration test — real LLM, skipped by default
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY")),
    reason="requires a real LLM provider key + MEMORYMASTER_ENTITY_LLM=1",
)
@pytest.mark.skipif(
    os.environ.get("MEMORYMASTER_ENTITY_LLM_INTEGRATION", "0") != "1",
    reason="opt in via MEMORYMASTER_ENTITY_LLM_INTEGRATION=1",
)
def test_integration_real_llm_smoke():
    text = (
        "Colombero shipped the FastAPI migration on Thursday using "
        "gemini-3.1-flash-lite-preview and LangChain."
    )
    result = extract_llm(text)
    # Do not assert specific entities — LLMs drift. Just verify shape.
    assert isinstance(result, list)
    for ent in result:
        assert ent.kind in LLM_KINDS
        assert ent.surface
        assert ent.canonical_hint
