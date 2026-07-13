"""Red tests for automated writers that bypass the governed ingest gateway."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.security import scan_text_for_findings
from memorymaster.govern.jobs.compact_summaries import run
from memorymaster.stores.storage import SQLiteStore


def _synthetic_token() -> str:
    body = "".join(format((index * 11 + 5) % 16, "x") for index in range(40))
    token = "".join(("gh", "p_", body))
    assert "github_token" in scan_text_for_findings(token)
    return token


def _create_archived_source(store: SQLiteStore) -> None:
    claim = store.create_claim(
        text="A benign archived claim for automated summary testing.",
        citations=[CitationInput(source="phase0-red-test", locator="case:compact-summary")],
        subject="write-gateway",
        predicate="source_fact",
        object_value="benign",
    )
    transition_claim(store, claim.id, to_status="confirmed", reason="test", event_type="transition")
    transition_claim(store, claim.id, to_status="stale", reason="test", event_type="decay")
    transition_claim(store, claim.id, to_status="archived", reason="test", event_type="compactor")


def _claim_and_citation_text(db_path: Path) -> str:
    with sqlite3.connect(db_path) as conn:
        claims = conn.execute(
            "SELECT text, idempotency_key, subject, predicate, object_value, source_agent, holder FROM claims"
        ).fetchall()
        citations = conn.execute("SELECT source, locator, excerpt FROM citations").fetchall()
    return "\n".join(str(value) for row in [*claims, *citations] for value in row if value is not None)


def test_compact_summary_output_never_persists_secret_shaped_llm_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QDRANT_URL", raising=False)
    db_path = tmp_path / "compact-summary-write-gateway.db"
    store = SQLiteStore(db_path)
    store.init_db()
    _create_archived_source(store)
    secret = _synthetic_token()
    response = json.dumps(
        {
            "summary_text": f"The generated summary included {secret}.",
            "subject": "write-gateway",
            "predicate": "summary_of",
            "object_value": "synthetic output",
            "confidence": 0.9,
        }
    )

    with patch("memorymaster.govern.jobs.compact_summaries._call_llm", return_value=response):
        result = run(store, provider="custom", min_cluster=1, dry_run=False)

    assert result.clusters_found == 1
    assert result.summaries_created + result.errors == 1
    assert secret not in _claim_and_citation_text(db_path)
