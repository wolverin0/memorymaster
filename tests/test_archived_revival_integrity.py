"""G-2: reconcile_integrity accepts the documented re-ingest revival and nothing else out of `archived`."""
from __future__ import annotations

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService

CIT = [CitationInput(source="test://g2", locator="x")]
TEXT = "Polluted eval fact number one about the zorblax cache invalidation retention window setting"


@pytest.fixture()
def svc(tmp_path):
    s = MemoryService(tmp_path / "g2.db", workspace_root=tmp_path)
    s.init_db()
    return s


def _transition_issues(svc):
    return svc.store.reconcile_integrity(fix=False, limit=1000)["issues"].get("transition_issues") or []


def test_revival_is_not_a_transition_issue(svc):
    a = svc.ingest(TEXT, CIT, scope="project:g2", source_agent="eval-bot")
    svc.store.archive_by_source("eval-bot", dry_run=False)
    again = svc.ingest(TEXT, CIT, scope="project:g2", source_agent="eval-bot")
    assert again.id == a.id and svc.store.get_claim(a.id, include_citations=False).status == "candidate"
    assert _transition_issues(svc) == []


def test_other_archived_exits_are_still_reported(svc):
    a = svc.ingest(TEXT, CIT, scope="project:g2", source_agent="eval-bot")
    svc.store.archive_by_source("eval-bot", dry_run=False)
    svc.store.record_event(claim_id=a.id, event_type="transition", from_status="archived", to_status="confirmed",
                           details="hand made")
    svc.store.record_event(claim_id=a.id, event_type="transition", from_status="archived", to_status="candidate",
                           details="not a revival")
    issues = _transition_issues(svc)
    assert sorted(i["to_status"] for i in issues) == ["candidate", "confirmed"]
