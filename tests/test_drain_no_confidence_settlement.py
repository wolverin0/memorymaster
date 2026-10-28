"""T-1863 F5 -- the curation drain must not retire a correction by confidence.

Review repro: old "datastore uses SQLite" confirmed at 0.79; newer PostgreSQL
correction at 0.69 held conflicted. The drain ran ``_pick_winner`` (confidence
first), so the correction was superseded by the very claim it corrects.
Different-value contradictions stay conflicted for review unless an explicit
supersession link says which side replaces which.
"""
from __future__ import annotations

import pytest

from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.jobs import curation_drain


@pytest.fixture()
def svc(tmp_path):
    s = MemoryService(str(tmp_path / "f5.db"), workspace_root=str(tmp_path))
    s.init_db()
    return s


def _claim(svc, text, object_value, confidence, **kwargs):
    return svc.ingest(
        text=text,
        citations=[CitationInput(source="session://t", locator="turn", excerpt=text[:30])],
        subject="datastore", predicate="engine", object_value=object_value,
        scope="project:x", confidence=confidence, source_agent="claude-session",
        **kwargs,
    ).id


def _link(svc, replacement_id, predecessor_id):
    """Persist an explicit supersession link (ingest only files a proposal)."""
    with svc.store.connect() as conn:
        conn.execute(
            "UPDATE claims SET supersedes_claim_id = ? WHERE id = ?",
            (predecessor_id, replacement_id),
        )


def _pair(svc, *, new_object="PostgreSQL", new_supersedes=False):
    old = _claim(svc, "The datastore uses SQLite.", "SQLite", 0.79)
    transition_claim(svc.store, old, "confirmed", reason="fixture")
    new = _claim(svc, "Correction: the datastore uses PostgreSQL.", new_object, 0.69)
    if new_supersedes:
        _link(svc, new, old)
    transition_claim(svc.store, new, "conflicted", reason="fixture")
    return old, new


def _status(svc, claim_id):
    return svc.store.get_claim(claim_id, include_citations=False).status


def test_correction_is_not_retired_by_higher_confidence_rival(svc):
    old, new = _pair(svc)

    result = curation_drain.drain_conflicts(svc, apply=True)

    assert _status(svc, new) == "conflicted", "correction was settled by confidence alone"
    assert _status(svc, old) == "confirmed"
    assert result["resolved_lost"] == 0 and result["resolved_won"] == 0
    assert result["held_for_review"] == 1


def test_lower_confidence_newcomer_is_not_promoted_by_confidence_either(svc):
    old = _claim(svc, "The datastore uses SQLite.", "SQLite", 0.3)
    transition_claim(svc.store, old, "confirmed", reason="fixture")
    new = _claim(svc, "Correction: the datastore uses PostgreSQL.", "PostgreSQL", 0.95)
    transition_claim(svc.store, new, "conflicted", reason="fixture")

    result = curation_drain.drain_conflicts(svc, apply=True)

    assert (_status(svc, old), _status(svc, new)) == ("confirmed", "conflicted")
    assert result["held_for_review"] == 1


def test_explicit_supersession_link_still_settles_for_the_replacement(svc):
    old, new = _pair(svc, new_supersedes=True)  # old .79 > new .69, but linked

    result = curation_drain.drain_conflicts(svc, apply=True)

    assert result["resolved_won"] == 1
    assert _status(svc, new) == "confirmed"
    assert _status(svc, old) == "superseded"


def test_explicit_link_from_rival_retires_the_conflicted_claim(svc):
    # The confirmed claim declares it replaces the conflicted one.
    stale = _claim(svc, "The datastore used MySQL.", "MySQL", 0.99)
    transition_claim(svc.store, stale, "conflicted", reason="fixture")
    current = _claim(svc, "The datastore uses PostgreSQL.", "PostgreSQL", 0.1)
    _link(svc, current, stale)
    transition_claim(svc.store, current, "confirmed", reason="fixture")

    result = curation_drain.drain_conflicts(svc, apply=True)

    assert result["resolved_lost"] == 1
    assert _status(svc, stale) == "superseded"
    assert _status(svc, current) == "confirmed"


def test_dry_run_reports_held_for_review_without_mutating(svc):
    old, new = _pair(svc)

    result = curation_drain.drain_conflicts(svc, apply=False)

    assert result["held_for_review"] == 1 and result["dry_run"] is True
    assert (_status(svc, old), _status(svc, new)) == ("confirmed", "conflicted")


def test_held_conflicts_do_not_starve_a_resolvable_one_behind_them(svc):
    """T-1877 (review N6): the bounded fetch used to be spent on the same top-ranked held entries every run."""
    def held_pair(n):
        rival = svc.ingest(
            text=f"Service {n} runs on SQLite.", citations=[CitationInput(source="session://t", locator="x")],
            subject=f"service-{n}", predicate="engine", object_value="SQLite", scope="project:x",
            confidence=0.5, source_agent="claude-session",
        ).id
        transition_claim(svc.store, rival, "confirmed", reason="fixture")
        other = svc.ingest(
            text=f"Service {n} runs on PostgreSQL.", citations=[CitationInput(source="session://t", locator="y")],
            subject=f"service-{n}", predicate="engine", object_value="PostgreSQL", scope="project:x",
            confidence=0.95, source_agent="claude-session",
        ).id
        transition_claim(svc.store, other, "conflicted", reason="fixture")

    for n in range(4):
        held_pair(n)
    old = _claim(svc, "The datastore uses SQLite.", "SQLite", 0.5)
    transition_claim(svc.store, old, "confirmed", reason="fixture")
    new = _claim(svc, "Correction: the datastore uses PostgreSQL.", "PostgreSQL", 0.1)
    _link(svc, new, old)
    transition_claim(svc.store, new, "conflicted", reason="fixture")

    result = curation_drain.drain_conflicts(svc, limit=2, apply=True)

    assert _status(svc, new) == "confirmed" and _status(svc, old) == "superseded"
    assert result["resolved_won"] == 1 and result["held_for_review"] == 4
