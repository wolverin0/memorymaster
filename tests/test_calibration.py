from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memorymaster.surfaces.cli import main
from memorymaster.jobs.calibration import compute_priors, run
from memorymaster.models import CitationInput
from memorymaster.service import MemoryService


NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
IN_WINDOW = "2026-05-01T00:00:00+00:00"
OUT_OF_WINDOW = "2026-01-01T00:00:00+00:00"


@pytest.fixture()
def service(tmp_path: Path) -> MemoryService:
    svc = MemoryService(str(tmp_path / "calibration.db"), workspace_root=tmp_path)
    svc.init_db()
    return svc


def _claim(service: MemoryService, text: str, claim_type: str | None):
    return service.ingest(
        text=text,
        citations=[CitationInput(source="test")],
        claim_type=claim_type,
        idempotency_key=text,
    )


def _insert_validator_event(
    service: MemoryService,
    claim_id: int,
    *,
    to_status: str | None = None,
    details: str | None = None,
    created_at: str = IN_WINDOW,
) -> None:
    with service.store.connect() as conn:
        conn.execute(
            """
            INSERT INTO events (
                claim_id, event_type, from_status, to_status, details, payload_json, created_at
            )
            VALUES (?, 'validator', 'candidate', ?, ?, ?, ?)
            """,
            (
                claim_id,
                to_status,
                details,
                json.dumps({"score": 0.75, "citation_count": 1}),
                created_at,
            ),
        )
        conn.commit()


def test_compute_priors_groups_validator_events_by_claim_type(service: MemoryService) -> None:
    fact_a = _claim(service, "fact success", "fact")
    fact_b = _claim(service, "fact pending", "fact")
    decision_a = _claim(service, "decision success", "decision")
    decision_b = _claim(service, "decision conflict", "decision")
    old_fact = _claim(service, "old fact success", "fact")
    uncategorized = _claim(service, "missing type success", None)

    _insert_validator_event(service, fact_a.id, to_status="confirmed")
    _insert_validator_event(service, fact_b.id, details="validation_pending_more_evidence")
    _insert_validator_event(service, decision_a.id, details="revalidation_passed")
    _insert_validator_event(service, decision_b.id, to_status="conflicted")
    _insert_validator_event(service, old_fact.id, to_status="confirmed", created_at=OUT_OF_WINDOW)
    _insert_validator_event(service, uncategorized.id, to_status="confirmed")

    report = compute_priors(service.store, window_days=90, now=NOW)
    by_type = {row["claim_type"]: row for row in report["priors"]}

    assert report["total_attempts"] == 5
    assert report["total_validated"] == 3
    assert report["global_empirical_validation_rate"] == 0.6
    assert by_type["fact"]["validation_attempts"] == 2
    assert by_type["fact"]["validated"] == 1
    assert by_type["fact"]["recommended_initial_confidence"] == 0.5
    assert by_type["decision"]["empirical_validation_rate"] == 0.5
    assert by_type["uncategorized"]["recommended_initial_confidence"] == 1.0


def test_run_writes_report_without_mutating_claims_or_events(service: MemoryService, tmp_path: Path) -> None:
    claim = _claim(service, "report-only claim", "constraint")
    _insert_validator_event(service, claim.id, to_status="confirmed")
    before_events = service.list_events(limit=1000)

    output = tmp_path / "calibration-priors.json"
    report = run(service.store, window_days=90, output=output)

    assert output.exists()
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["priors"] == report["priors"]
    assert service.store.get_claim(claim.id, include_citations=False).status == "candidate"
    assert service.list_events(limit=1000) == before_events


def test_cli_recompute_confidence_priors_writes_json(tmp_path: Path, capsys) -> None:
    db = tmp_path / "cli-calibration.db"
    output = tmp_path / "docs" / "calibration-priors-2026-05-11.json"

    assert main(["--db", str(db), "init-db"]) == 0
    svc = MemoryService(str(db), workspace_root=tmp_path)
    claim = _claim(svc, "cli fact success", "fact")
    _insert_validator_event(svc, claim.id, to_status="confirmed")

    capsys.readouterr()
    rc = main(
        [
            "--json",
            "--db",
            str(db),
            "recompute-confidence-priors",
            "--window-days",
            "90",
            "--output",
            str(output),
        ]
    )

    parsed = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert output.exists()
    assert parsed["ok"] is True
    assert parsed["data"]["output"] == str(output)
    assert parsed["data"]["priors"][0]["claim_type"] == "fact"
