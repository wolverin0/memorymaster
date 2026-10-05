"""Moving a claim to the scope it belongs to keeps its content and leaves an audit trail.

Curation 2026-10-05: thousands of project-specific claims were written to
project:py-apps. A move copies the claim (text, triple, citations, confidence,
lifecycle status) into the target scope and supersedes the original, so the
lineage stays readable and the move can be reversed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memorymaster.core import lifecycle
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern import rescope
from memorymaster.recall.qdrant_outbox import ENV_OUTBOX_DIR


@pytest.fixture()
def store(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(ENV_OUTBOX_DIR, str(tmp_path / "outbox"))
    monkeypatch.delenv("QDRANT_URL", raising=False)
    svc = MemoryService(tmp_path / "r.db", workspace_root=tmp_path)
    svc.init_db()
    return svc.store


def _claim(store, status: str):
    claim = store.create_claim(text="whatsappbot reclamos are triaged before 9am", subject="whatsappbot",
                               predicate="triage_time", object_value="before 9am", confidence=0.83,
                               citations=[CitationInput(source="session://x", locator="turn:4", excerpt="triage")],
                               scope="project:py-apps")
    for step in {"candidate": (), "confirmed": ("confirmed",), "stale": ("confirmed", "stale")}[status]:
        lifecycle.transition_claim(store, claim.id, step, reason="fixture", event_type="validator")
    return claim.id


@pytest.mark.parametrize("status", ["candidate", "confirmed", "stale"])
def test_a_move_copies_the_claim_and_supersedes_the_original(store, status) -> None:
    old_id = _claim(store, status)
    new_id = rescope.rescope_claim(store, old_id, "project:whatsappbot", reason="curation test")
    old, new = store.get_claim(old_id, include_citations=True), store.get_claim(new_id, include_citations=True)
    assert (new.scope, new.status, new.text, new.subject, new.object_value) == \
        ("project:whatsappbot", status, old.text, old.subject, old.object_value)
    assert round(new.confidence, 3) == 0.83
    assert [(c.source, c.locator, c.excerpt) for c in new.citations] == [("session://x", "turn:4", "triage")]
    assert (old.status, old.replaced_by_claim_id, new.supersedes_claim_id) == ("superseded", new_id, old_id)


def test_retired_claims_and_same_scope_moves_are_refused(store) -> None:
    claim_id = _claim(store, "confirmed")
    with pytest.raises(ValueError, match="already"):
        rescope.rescope_claim(store, claim_id, "project:py-apps", reason="noop")
    lifecycle.transition_claim(store, claim_id, "archived", reason="fixture", event_type="compactor")
    with pytest.raises(ValueError, match="archived"):
        rescope.rescope_claim(store, claim_id, "project:whatsappbot", reason="retired")
