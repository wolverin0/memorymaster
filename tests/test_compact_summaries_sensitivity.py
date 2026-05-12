"""Sensitivity tests for compact_summaries LLM egress."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from memorymaster.jobs.compact_summaries import run
from memorymaster.lifecycle import transition_claim
from memorymaster.models import CitationInput
from memorymaster.storage import SQLiteStore


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "test.db"
    s = SQLiteStore(db_path)
    s.init_db()
    return s


def _create_archived_claim(store, text: str):
    claim = store.create_claim(
        text=text,
        citations=[CitationInput(source="test")],
        subject="legacy-secret",
        predicate="contains",
        object_value="synthetic test fixture",
    )
    transition_claim(store, claim.id, to_status="confirmed", reason="test", event_type="transition")
    transition_claim(store, claim.id, to_status="stale", reason="test", event_type="decay")
    transition_claim(store, claim.id, to_status="archived", reason="test", event_type="compactor")
    return store.get_claim(claim.id)


@patch("memorymaster.jobs.compact_summaries._call_llm")
def test_compact_summaries_redacts_claim_text_before_llm_call(mock_llm, store):
    raw_secret = "sk-fake-test-1234567890abcdefghij"
    _create_archived_claim(
        store,
        f"Legacy archived claim contains OPENAI_API_KEY={raw_secret}",
    )

    captured_prompts: list[str] = []

    def capture_prompt(provider, api_key, model, prompt, base_url, **kwargs):
        captured_prompts.append(prompt)
        return json.dumps({
            "summary_text": "Legacy archived claim had a redacted secret marker.",
            "subject": "legacy-secret",
            "predicate": "summary_of",
            "object_value": "redacted legacy secret marker",
            "confidence": 0.9,
        })

    mock_llm.side_effect = capture_prompt

    result = run(
        store,
        provider="gemini",
        api_key="fake-key",
        min_cluster=1,
        dry_run=False,
    )

    assert result.errors == 0
    assert captured_prompts
    assert raw_secret not in captured_prompts[0]
