"""Tests for memorymaster/entity_extractor.py.

Drives the extractor against the labeled fixture
``tests/fixtures/entity_extraction_eval.jsonl`` and asserts per-kind
recall ≥ 0.9 and false-positive rate ≤ 0.1. Also includes a handful of
adversarial micro-cases (timestamps not caught as ports, etc.).
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from memorymaster.entity_extractor import Entity, extract_patterns

FIXTURE = Path(__file__).parent / "fixtures" / "entity_extraction_eval.jsonl"

KINDS = ("file", "env-var", "service", "port", "commit", "tool")


def _load_fixture() -> list[dict]:
    rows: list[dict] = []
    with FIXTURE.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _canonical_set(entities: list[Entity]) -> set[tuple[str, str]]:
    return {(e.kind, e.canonical_hint.lower()) for e in entities}


def _expected_canonical(expected: list[dict]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for e in expected:
        surface = e["surface"].strip()
        kind = e["kind"]
        if kind == "port":
            # canonical = ":NNNN"
            import re as _re

            m = _re.search(r"\d+", surface)
            canonical = f":{m.group(0)}" if m else surface
        elif kind == "file":
            canonical = surface.lower().rstrip("/")
        elif kind == "service":
            canonical = surface.lower()
        elif kind == "tool":
            canonical = surface if surface.startswith("mcp__") else surface.lower()
        else:
            canonical = surface
        out.add((kind, canonical.lower()))
    return out


@pytest.fixture(scope="module")
def fixture_rows() -> list[dict]:
    rows = _load_fixture()
    assert len(rows) >= 100, f"fixture must have ≥100 labeled rows, got {len(rows)}"
    return rows


def test_fixture_has_all_kinds(fixture_rows: list[dict]) -> None:
    kinds_found: dict[str, int] = defaultdict(int)
    for row in fixture_rows:
        for e in row["expected_entities"]:
            kinds_found[e["kind"]] += 1
    for kind in KINDS:
        assert kinds_found[kind] >= 5, f"kind {kind!r} has <5 examples: {kinds_found[kind]}"


@pytest.mark.parametrize("kind", KINDS)
def test_recall_and_fpr_per_kind(fixture_rows: list[dict], kind: str) -> None:
    """For each kind, recall ≥ 0.9 and FPR ≤ 0.1.

    recall := true_positives / (true_positives + false_negatives)
    fpr    := false_positives / max(total_predicted_this_kind, 1)
    """
    tp = 0
    fn = 0
    fp = 0
    for row in fixture_rows:
        expected = _expected_canonical(row["expected_entities"])
        predicted = _canonical_set(extract_patterns(row["text"]))

        expected_this = {(k, c) for (k, c) in expected if k == kind}
        predicted_this = {(k, c) for (k, c) in predicted if k == kind}

        tp += len(expected_this & predicted_this)
        fn += len(expected_this - predicted_this)
        fp += len(predicted_this - expected_this)

    recall = tp / (tp + fn) if (tp + fn) else 1.0
    total_predicted = tp + fp
    fpr = fp / total_predicted if total_predicted else 0.0

    assert recall >= 0.9, f"{kind}: recall={recall:.3f} tp={tp} fn={fn}"
    assert fpr <= 0.1, f"{kind}: fpr={fpr:.3f} fp={fp} tp={tp}"


def test_dedup_within_single_text() -> None:
    text = "GEMINI_API_KEY GEMINI_API_KEY is used twice. GEMINI_API_KEY again."
    out = extract_patterns(text)
    env_vars = [e for e in out if e.kind == "env-var"]
    assert len(env_vars) == 1


def test_empty_text_returns_empty() -> None:
    assert extract_patterns("") == []
    assert extract_patterns("   ") == []


def test_timestamp_not_treated_as_port() -> None:
    # "02:00" is a timestamp, must not trigger the :NNNN port pattern.
    out = extract_patterns("Scheduled at 02:00 UTC for the release.")
    assert not any(e.kind == "port" for e in out)


def test_mcp_tool_id_extracted() -> None:
    out = extract_patterns("Called mcp__memorymaster__ingest_claim successfully.")
    tools = [e for e in out if e.kind == "tool"]
    assert any(e.surface == "mcp__memorymaster__ingest_claim" for e in tools)


def test_commit_short_hex_requires_context() -> None:
    # Bare "abc1234" with no commit context should NOT be flagged.
    out = extract_patterns("The variable abc1234 holds an opaque id.")
    assert not any(e.kind == "commit" for e in out)
    # With context it should.
    out2 = extract_patterns("Reverted commit abc1234 because of the bug.")
    assert any(e.kind == "commit" and e.canonical_hint == "abc1234" for e in out2)


def test_service_blocklist() -> None:
    out = extract_patterns("The feature is up-to-date and end-to-end tested.")
    services = [e for e in out if e.kind == "service"]
    assert not services


def test_extract_patterns_returns_entity_dataclass() -> None:
    out = extract_patterns("Fixed bug in src/agents/auth.py")
    assert out
    assert all(isinstance(e, Entity) for e in out)
    assert out[0].kind == "file"
    assert out[0].canonical_hint == "src/agents/auth.py"
