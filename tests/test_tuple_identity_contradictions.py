"""T-1863 F6 -- contradictions must not fall between the checks.

Review repro: ``API / authentication / None`` "allowed without header" and
``api / authentication / None`` "rejected" both ended confirmed, because the
lookup was exact-case, the differing-object check needed populated objects, the
semantic probe skipped any same-tuple pair as "handled", and the steward treated
two missing objects as equal.
"""
from __future__ import annotations

import pytest

from memorymaster.core.models import CitationInput, normalize_identity_part
from memorymaster.core.service import MemoryService
from memorymaster.govern import contradiction_probe
from memorymaster.govern.jobs import validator
from memorymaster.govern.steward import _find_replacement_or_conflict, _relation_index

LONG_A = "The API accepts requests that are allowed without an authentication header in staging. " * 3
LONG_B = "The api rejects every request that arrives without an authentication header at all. " * 3


@pytest.fixture()
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(validator, "_classifier_promote_decision", lambda *_: None)
    svc = MemoryService(str(tmp_path / "f6.db"), workspace_root=str(tmp_path))
    svc.init_db()
    return svc


def _ingest(svc, text, subject, predicate="authentication", object_value=None):
    return svc.ingest(
        text=text,
        citations=[CitationInput(source="fixture:a"), CitationInput(source="fixture:b")],
        subject=subject,
        predicate=predicate,
        object_value=object_value,
        scope="project:test",
        confidence=0.9,
        source_agent="claude-session",
    )


def _statuses(svc, *claims):
    return [svc.store.get_claim(c.id).status for c in claims]


def test_normalize_identity_part():
    assert normalize_identity_part("  API\tGateway ") == "api gateway"
    assert normalize_identity_part(None) == ""


def test_store_lookup_is_case_and_whitespace_insensitive(service):
    a = _ingest(service, LONG_A, "API")
    validator.run(service.store)
    assert _statuses(service, a) == ["confirmed"]

    found = service.store.find_confirmed_by_tuple(
        subject=" api ", predicate="Authentication", scope="project:test"
    )

    assert [c.id for c in found] == [a.id]


@pytest.mark.parametrize("subject_b", ["api", "API"])
def test_opposite_assertions_with_missing_objects_never_both_confirm(service, subject_b):
    a = _ingest(service, LONG_A, "API")
    b = _ingest(service, LONG_B, subject_b)

    validator.run(service.store)

    statuses = _statuses(service, a, b)
    assert statuses.count("confirmed") <= 1, statuses
    assert "conflicted" in statuses, statuses


def test_steward_missing_objects_are_not_the_same_value(service):
    a = _ingest(service, LONG_A, "API")
    b = _ingest(service, LONG_B, "api")
    validator.run(service.store)
    first = next(c for c in (a, b) if service.store.get_claim(c.id).status == "confirmed")
    other = b if first is a else a
    index = _relation_index([service.store.get_claim(first.id), service.store.get_claim(other.id)])

    _replacement, has_conflict = _find_replacement_or_conflict(
        service.store.get_claim(other.id), index
    )

    assert has_conflict is True


def test_probe_does_not_skip_same_tuple_pair_without_objects(service):
    a = _ingest(service, LONG_A, "API")
    b = _ingest(service, LONG_B, "api")

    assert contradiction_probe._prefiltered(a, b) is False


def test_probe_still_skips_same_tuple_pair_with_differing_objects(service):
    a = _ingest(service, LONG_A, "API", object_value="open")
    b = _ingest(service, LONG_B, "api", object_value="closed")

    assert contradiction_probe._prefiltered(a, b) is True
