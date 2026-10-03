"""T-0773: rapidfuzz near-duplicates are proposed to the steward, never merged.

Measured on the live DB (2026-10-03): token_set_ratio >= 85 alone gave 5/30
precision; the false pairs were one template with different identifiers.
Rejecting identifier differences left 9 pairs, 9/9 true duplicates.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("rapidfuzz")

from memorymaster.core import lifecycle  # noqa: E402
from memorymaster.core.models import CitationInput  # noqa: E402
from memorymaster.core.service import MemoryService  # noqa: E402
from memorymaster.govern import fuzzy_dedupe  # noqa: E402
from memorymaster.govern.jobs import curation_drain  # noqa: E402
from memorymaster.govern.steward import (  # noqa: E402
    JevProposalOperatorOnly,
    list_steward_proposals,
    resolve_steward_proposal,
)
from memorymaster.recall import qdrant_outbox  # noqa: E402

KEEP = "Committing secrets is a critical security failure that compromises codebase integrity."
PARAPHRASE = ("Committing secrets directly into the repository constitutes a critical security failure "
              "that compromises codebase integrity.")
TEMPLATE_A = ("FlowLens admin includes lead-intake insight cards from stored pm_lead_arrival answers, "
              "and verify:local-review passes locally with 9/9 E2E checks")
TEMPLATE_B = ("FlowLens admin includes lead-tracking insight cards from stored pm_lead_tracking answers, "
              "and verify:local-review passes locally with 9/9 E2E checks")


def test_identifier_differences_mark_different_entities():
    assert fuzzy_dedupe.differs_by_identifier(TEMPLATE_A, TEMPLATE_B)
    assert fuzzy_dedupe.differs_by_identifier("PR #36 adds a queue", "PR #37 adds a queue")
    assert not fuzzy_dedupe.differs_by_identifier(KEEP, PARAPHRASE)


def test_pairs_are_paraphrases_within_one_scope_beyond_jaccard():
    rows = [(1, "project:a", KEEP), (2, "project:a", PARAPHRASE), (3, "project:b", PARAPHRASE),
            (4, "project:a", TEMPLATE_A), (5, "project:a", TEMPLATE_B), (6, "project:a", KEEP + " ")]
    pairs = fuzzy_dedupe.find_pairs(rows)
    assert [(p.keep_id, p.newer_id, p.scope) for p in pairs] == [(1, 2, "project:a"), (2, 6, "project:a")]


@pytest.fixture()
def service(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(qdrant_outbox.ENV_OUTBOX_DIR, str(tmp_path / "qdrant-outbox"))
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.setenv("MEMORYMASTER_DECISIONS_DB", str(tmp_path / "decisions.db"))
    svc = MemoryService(tmp_path / "fuzzy.db", workspace_root=tmp_path)
    svc.init_db()
    return svc


def _confirmed(service: MemoryService, text: str):
    claim = service.ingest(text, [CitationInput(source="test://fuzzy")], scope="project:mm")
    lifecycle.transition_claim(service.store, claim.id, "confirmed", reason="fixture", event_type="validator")
    return claim.id


def _status(service: MemoryService, claim_id: int) -> str:
    return service.store.get_claim(claim_id, include_citations=False).status


def _fuzzy_proposals(service: MemoryService) -> list[dict]:
    return [p for p in list_steward_proposals(service, limit=50, include_resolved=False)
            if (p.get("payload") or {}).get("source") == "fuzzy"]


def test_a_paraphrase_files_one_proposal_and_changes_no_status(service):
    keep, newer = _confirmed(service, KEEP), _confirmed(service, PARAPHRASE)
    assert fuzzy_dedupe.run(service.store) == {"pairs": 1, "proposed": 1, "skipped": {}}
    [proposal] = _fuzzy_proposals(service)
    assert proposal["claim_id"] == newer and proposal["payload"]["replaced_by_claim_id"] == keep
    assert (_status(service, keep), _status(service, newer)) == ("confirmed", "confirmed")
    assert fuzzy_dedupe.run(service.store)["skipped"] == {"proposal_exists": 1}


def test_automation_never_approves_it_and_the_operator_can(service):
    keep, newer = _confirmed(service, KEEP), _confirmed(service, PARAPHRASE)
    fuzzy_dedupe.run(service.store)
    drain = curation_drain.drain_proposals(service, apply=True)
    assert drain["approved"] == 0 and drain["kept_for_operator"] == 1, drain
    assert _status(service, newer) == "confirmed"
    [proposal] = _fuzzy_proposals(service)
    with pytest.raises(JevProposalOperatorOnly):
        resolve_steward_proposal(service, action="approve", proposal_event_id=proposal["proposal_event_id"],
                                 apply_on_approve=True, actor="automation")
    resolve_steward_proposal(service, action="approve", proposal_event_id=proposal["proposal_event_id"],
                             apply_on_approve=True, actor="operator")
    after = service.store.get_claim(newer, include_citations=False)
    assert (after.status, after.replaced_by_claim_id) == ("superseded", keep)


def test_off_switch_and_cap(service, monkeypatch):
    _confirmed(service, KEEP), _confirmed(service, PARAPHRASE)
    monkeypatch.setenv(fuzzy_dedupe.ENABLED_ENV, "0")
    assert fuzzy_dedupe.run(service.store) == {"stopped": "disabled"}
    monkeypatch.delenv(fuzzy_dedupe.ENABLED_ENV)
    assert fuzzy_dedupe.run(service.store, limit=0) == {"stopped": "cap_zero"}
    assert _fuzzy_proposals(service) == []
