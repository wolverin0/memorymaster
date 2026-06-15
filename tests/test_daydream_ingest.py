from __future__ import annotations

import json
from pathlib import Path

import pytest

from memorymaster.govern.jobs.daydream_ingest import ingest_insights
from memorymaster.core.service import MemoryService


@pytest.fixture()
def service(tmp_path: Path) -> MemoryService:
    svc = MemoryService(str(tmp_path / "memorymaster.db"))
    svc.init_db()
    return svc


def _write_insight(
    directory: Path,
    name: str,
    *,
    score: float,
    title: str,
    synthesis: str | None = None,
) -> Path:
    path = directory / name
    path.write_text(
        json.dumps(
            {
                "connection": f"{title} connection",
                "synthesis": synthesis or f"{title} synthesis",
                "implication": f"{title} implication",
                "suggested_title": title,
                "metadata": {
                    "date": "2026-05-16",
                    "references": [
                        {"title": "Source A", "path": "notes/source-a.md"},
                        {"title": "Source B", "path": "notes/source-b.md"},
                    ],
                    "scores": {"average": score},
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _claims(service: MemoryService):
    return service.list_claims(status="candidate", limit=20, allow_sensitive=True)


def test_ingests_above_threshold(service: MemoryService, tmp_path: Path):
    _write_insight(tmp_path, "one.json", score=8.0, title="One")
    _write_insight(tmp_path, "two.json", score=6.0, title="Two")
    _write_insight(tmp_path, "three.json", score=9.5, title="Three")

    result = ingest_insights(service, tmp_path, min_score=7.0)

    assert result["ingested"] == 2
    assert result["skipped"] == 1
    assert len(_claims(service)) == 2


def test_dry_run_no_writes(service: MemoryService, tmp_path: Path):
    _write_insight(tmp_path, "one.json", score=8.0, title="One")
    _write_insight(tmp_path, "two.json", score=6.0, title="Two")
    _write_insight(tmp_path, "three.json", score=9.5, title="Three")

    result = ingest_insights(service, tmp_path, min_score=7.0, dry_run=True)

    assert result["ingested"] == 2
    assert result["skipped"] == 1
    assert _claims(service) == []


def test_idempotent(service: MemoryService, tmp_path: Path):
    _write_insight(tmp_path, "one.json", score=8.0, title="One")
    _write_insight(tmp_path, "two.json", score=9.5, title="Two")

    first = ingest_insights(service, tmp_path, min_score=7.0)
    second = ingest_insights(service, tmp_path, min_score=7.0)

    assert first["ingested"] == 2
    assert second["ingested"] == 0
    assert second["skipped"] == 2
    assert len(_claims(service)) == 2


def test_handles_malformed_json(service: MemoryService, tmp_path: Path):
    _write_insight(tmp_path, "one.json", score=8.0, title="One")
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")

    result = ingest_insights(service, tmp_path, min_score=7.0)

    assert result["ingested"] == 1
    assert result["skipped"] == 1
    assert len(result["errors"]) == 1
    assert "bad.json" in result["errors"][0]


def test_uses_correct_claim_type_and_confidence(service: MemoryService, tmp_path: Path):
    _write_insight(tmp_path, "one.json", score=8.0, title="One")

    ingest_insights(service, tmp_path, min_score=7.0)

    [claim] = _claims(service)
    assert claim.claim_type == "hypothesis"
    assert claim.confidence == 0.5
    assert claim.source_agent == "daydream"


def test_citations_link_to_source_notes(service: MemoryService, tmp_path: Path):
    _write_insight(tmp_path, "one.json", score=8.0, title="One")

    ingest_insights(service, tmp_path, min_score=7.0)

    [claim] = _claims(service)
    assert [citation.locator for citation in claim.citations] == [
        "notes/source-a.md",
        "notes/source-b.md",
    ]
