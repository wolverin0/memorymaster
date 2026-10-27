"""T-1863 F7 -- a no-op maintenance write must not reset the decay clock.

Decay ages a claim from ``updated_at``. ``set_confidence`` used to always write
``updated_at = now`` even when the confidence did not move, and the cycle runs
deterministic/validator (which re-write the same value) BEFORE decay, so a
claim that was never touched never decayed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.jobs import deterministic

OLD = "2020-01-01T00:00:00+00:00"


def _svc(tmp_path: Path) -> MemoryService:
    svc = MemoryService(tmp_path / "f7.db", workspace_root=tmp_path)
    svc.init_db()
    return svc


def _aged_claim(svc: MemoryService):
    claim = svc.ingest(
        text="a claim with no deterministic predicate",
        citations=[CitationInput(source="test", locator="loc")],
        source_agent="test-agent",
    )
    with svc.store.connect() as conn:
        conn.execute("UPDATE claims SET updated_at = ? WHERE id = ?", (OLD, claim.id))
    return svc.store.get_claim(claim.id)


@pytest.mark.unit
@pytest.mark.parametrize("details", [None, "deterministic_adjust=+0.000"])
def test_noop_set_confidence_keeps_updated_at(tmp_path: Path, details) -> None:
    svc = _svc(tmp_path)
    claim = _aged_claim(svc)

    svc.store.set_confidence(claim.id, claim.confidence, details=details)

    assert svc.store.get_claim(claim.id).updated_at == OLD


@pytest.mark.unit
def test_real_set_confidence_still_bumps_updated_at(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    claim = _aged_claim(svc)

    svc.store.set_confidence(claim.id, max(0.0, claim.confidence - 0.2), details="decay_rate=0.1")

    assert svc.store.get_claim(claim.id).updated_at != OLD


@pytest.mark.unit
def test_deterministic_pass_does_not_rejuvenate_untouched_claim(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    claim = _aged_claim(svc)

    deterministic.run(svc.store, workspace_root=tmp_path, limit=50)

    assert svc.store.get_claim(claim.id).updated_at == OLD
