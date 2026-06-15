from __future__ import annotations

import json
from pathlib import Path

import pytest

from memorymaster.core.service import MemoryService
from memorymaster.govern.steward import run_steward


@pytest.fixture()
def service(tmp_path: Path) -> MemoryService:
    svc = MemoryService(tmp_path / "memorymaster.db", workspace_root=tmp_path / "workspace")
    svc.init_db()
    return svc


def _run_clean_steward(service: MemoryService, tmp_path: Path) -> dict:
    return run_steward(
        service,
        mode="manual",
        max_cycles=1,
        max_claims=10,
        max_proposals=10,
        max_probe_files=10,
        artifact_path=tmp_path / "steward_report.json",
    )


def _write_insight(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "20260516-linking-patterns.md").write_text(
        """---
type: daydream
created_date: 2026-05-16
scores:
  average: 8.5
source_notes:
  - [[notes/source-a]]
---
# Linking Patterns

> Connection between steward output and daydream insight closure

Steward cycles can close the loop by ingesting high-scoring daydream insights.

## Implication

Accepted insights become candidate hypotheses for later validation.
""",
        encoding="utf-8",
    )


def test_no_env_no_op(service: MemoryService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMORYMASTER_DAYDREAM_INGEST_DIR", raising=False)

    report = _run_clean_steward(service, tmp_path)

    assert report["cycles_completed"] == 1
    assert "daydream" not in report


def test_env_set_dir_missing_no_crash(
    service: MemoryService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing-daydreams"
    monkeypatch.setenv("MEMORYMASTER_DAYDREAM_INGEST_DIR", str(missing))

    report = _run_clean_steward(service, tmp_path)

    assert report["cycles_completed"] == 1
    assert report["daydream"] == {"skipped": "dir-not-found", "path": str(missing)}


def test_env_set_dir_with_insights_ingests(
    service: MemoryService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    insights_dir = tmp_path / "Daydreams"
    _write_insight(insights_dir)
    monkeypatch.setenv("MEMORYMASTER_DAYDREAM_INGEST_DIR", str(insights_dir))

    report = _run_clean_steward(service, tmp_path)

    assert report["daydream"]["ingested"] == 1
    claims = service.list_claims(status="candidate", limit=20, allow_sensitive=True)
    assert len(claims) == 1
    assert claims[0].source_agent == "daydream"
    assert claims[0].claim_type == "hypothesis"


def test_ingest_exception_does_not_break_steward(
    service: MemoryService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    insights_dir = tmp_path / "Daydreams"
    insights_dir.mkdir()
    monkeypatch.setenv("MEMORYMASTER_DAYDREAM_INGEST_DIR", str(insights_dir))

    def raise_ingest(*_args: object, **_kwargs: object) -> dict:
        raise RuntimeError("boom from daydream ingest")

    monkeypatch.setattr("memorymaster.govern.jobs.daydream_ingest.ingest_insights", raise_ingest)

    report = _run_clean_steward(service, tmp_path)

    assert report["cycles_completed"] == 1
    assert report["cycles"][0]["budget"]["claims_scanned"] == 0
    assert json.dumps(report["cycles"])
    assert report["daydream"] == {"error": "boom from daydream ingest"}
