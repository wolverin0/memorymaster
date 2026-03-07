from __future__ import annotations

import os
import tempfile
from pathlib import Path

from memorymaster.models import CitationInput
from memorymaster.service import MemoryService


def _case_db(prefix: str) -> Path:
    fd, raw = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".db", dir=".tmp_cases")
    os.close(fd)
    Path(raw).unlink(missing_ok=True)
    return Path(raw)


def _ingest(service: MemoryService, *, predicate: str, object_value: str) -> int:
    claim = service.ingest(
        text=f"{predicate} is {object_value}",
        citations=[CitationInput(source="session://chat", locator="turn-1", excerpt="value")],
        subject="test",
        predicate=predicate,
        object_value=object_value,
        confidence=0.6,
    )
    return int(claim.id)


def test_deterministic_validator_accepts_richer_valid_predicates() -> None:
    db = _case_db("sqlite-deterministic-valid")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    _ingest(service, predicate="ipv6", object_value="2001:db8::1")
    _ingest(service, predicate="cidr", object_value="10.20.0.0/16")
    _ingest(service, predicate="uuid", object_value="123e4567-e89b-42d3-a456-426614174000")
    _ingest(service, predicate="phone_number", object_value="+14155550100")
    _ingest(service, predicate="country_code", object_value="US")

    result = service.run_cycle(policy_mode="legacy", min_citations=1, min_score=0.4)
    deterministic = result["deterministic"]
    assert deterministic["hard_conflicted"] == 0
    checks = deterministic["predicate_checks"]
    assert checks["ipv6_checked"] >= 1
    assert checks["cidr_checked"] >= 1
    assert checks["uuid_checked"] >= 1
    assert checks["phone_checked"] >= 1
    assert checks["country_code_checked"] >= 1


def test_deterministic_validator_conflicts_invalid_richer_predicates() -> None:
    db = _case_db("sqlite-deterministic-invalid")
    service = MemoryService(db, workspace_root=Path.cwd())
    service.init_db()

    bad_ids = [
        _ingest(service, predicate="ipv6", object_value="2001:::1"),
        _ingest(service, predicate="cidr", object_value="300.1.1.0/24"),
        _ingest(service, predicate="uuid", object_value="not-a-uuid"),
        _ingest(service, predicate="phone_number", object_value="555-012"),
    ]

    result = service.run_cycle(policy_mode="legacy", min_citations=1, min_score=0.4)
    deterministic = result["deterministic"]
    assert deterministic["hard_conflicted"] >= 4

    for claim_id in bad_ids:
        claim = service.store.get_claim(claim_id, include_citations=False)
        assert claim is not None
        assert claim.status == "conflicted"

