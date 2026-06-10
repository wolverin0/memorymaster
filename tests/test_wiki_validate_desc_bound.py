"""Regression: DESCRIPTION_TOO_LONG must align with the 50-200 char schema.

WHY: the enforced wiki schema (AGENTS.md Boundaries) requires descriptions of
50-200 chars, but the validator's upper bound was 300, so 201-300-char
descriptions passed the gate while violating the spec. These tests anchor on the
documented boundary, not the constant, so they catch any future drift of the
gate away from the 50-200 contract.
"""
from __future__ import annotations

from memorymaster.knowledge.wiki_validate import _validate_fields


def _fields(desc: str) -> dict:
    return {"title": "t", "type": "note", "scope": "project:x", "description": desc}


def test_description_over_200_is_too_long():
    codes = _validate_fields(_fields("d" * 201), "body")
    assert "DESCRIPTION_TOO_LONG" in codes, codes


def test_description_exactly_200_passes():
    codes = _validate_fields(_fields("d" * 200), "body")
    assert "DESCRIPTION_TOO_LONG" not in codes, codes


def test_description_under_50_is_too_short():
    codes = _validate_fields(_fields("d" * 49), "body")
    assert "DESCRIPTION_TOO_SHORT" in codes, codes
