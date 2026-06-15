"""Tests for v3.9.0 F2 — MemPalace-inspired Layer-1 CamelCase extraction.

Adds `library_name` extraction in entity_extractor.extract_patterns. Catches
multi-cap CamelCase (MemPalace, OpenAI, OneSignal) and tech-suffixed names
(ChromaDB, FastAPI, NextJS). Skips stoplist (OneDrive, GitHub, etc.) and
substrings of already-extracted env-vars/services.
"""
from __future__ import annotations

from memorymaster.knowledge.entity_extractor import extract_patterns, _CAMEL_STOPLIST


def _kinds_for(text: str) -> list[tuple[str, str]]:
    """Return (kind, canonical_hint) for each entity found."""
    return [(e.kind, e.canonical_hint) for e in extract_patterns(text)]


def test_camelcase_multicap_extracted():
    """`MemPalace`, `OpenAI`, `OneSignal` are extracted as library_name."""
    out = _kinds_for("Inspired by MemPalace and OpenAI patterns we used OneSignal.")
    canonicals = {c for k, c in out if k == "library_name"}
    assert "mempalace" in canonicals
    assert "openai" in canonicals
    assert "onesignal" in canonicals


def test_camelcase_tech_suffix_extracted():
    """`ChromaDB`, `FastAPI`, `NextJS` are extracted via tech-suffix branch."""
    out = _kinds_for("We use ChromaDB for vectors, FastAPI for HTTP, NextJS for the front.")
    canonicals = {c for k, c in out if k == "library_name"}
    assert "chromadb" in canonicals
    assert "fastapi" in canonicals
    assert "nextjs" in canonicals


def test_stoplist_blocks_false_positives():
    """`OneDrive`, `GitHub`, `WhatsApp` are stoplisted and NOT extracted."""
    out = _kinds_for("Saved to OneDrive, pushed to GitHub, sent via WhatsApp.")
    canonicals = {c for k, c in out if k == "library_name"}
    assert "onedrive" not in canonicals
    assert "github" not in canonicals
    assert "whatsapp" not in canonicals


def test_envvar_substring_not_double_extracted():
    """`OpenAI` inside `OPENAI_API_KEY` should NOT also be extracted as library_name.
    The env-var canonical contains the substring."""
    out = _kinds_for("Set OPENAI_API_KEY in your .env file.")
    canonicals_lib = {c for k, c in out if k == "library_name"}
    # The env-var extractor catches OPENAI_API_KEY; the library_name dedup
    # should suppress a separate `openai` library_name entity.
    assert "openai" not in canonicals_lib


def test_no_camelcase_in_neutral_text():
    """Plain lowercase prose produces no library_name entities."""
    out = _kinds_for("the system uses sqlite as the default database backend.")
    canonicals = {c for k, c in out if k == "library_name"}
    assert canonicals == set()


def test_single_capword_not_matched():
    """`Apple` or `Bug` alone (one capitalised word) should NOT match."""
    out = _kinds_for("The Apple in the Bug report was a metaphor.")
    canonicals = {c for k, c in out if k == "library_name"}
    assert canonicals == set()


def test_stoplist_is_frozen_set():
    assert isinstance(_CAMEL_STOPLIST, frozenset)
    assert "OneDrive" in _CAMEL_STOPLIST
