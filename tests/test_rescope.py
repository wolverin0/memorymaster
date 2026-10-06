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


def test_a_moved_claim_keeps_its_age_so_ranking_does_not_treat_it_as_new(store) -> None:
    # Found 2026-10-05: copies made at 03:29 carried created_at/last_validated_at of
    # that moment, so freshness (anchored on last_validated_at) and recompute_tiers
    # (created < 7 days -> core) ranked month-old claims as brand new and pushed the
    # operational-review canary out of the top 5.
    old_id = _claim(store, "confirmed")
    with store.connect() as conn:
        conn.execute("UPDATE claims SET created_at = '2026-09-02T03:37:46+00:00', "
                     "last_validated_at = '2026-09-02T08:02:42+00:00', tier = 'working', "
                     "access_count = 4, last_accessed = '2026-09-20T10:00:00+00:00' WHERE id = ?", (old_id,))
        conn.commit()
    new = store.get_claim(rescope.rescope_claim(store, old_id, "project:whatsappbot", reason="age"),
                          include_citations=False)
    assert (new.created_at, new.last_validated_at, new.tier, new.access_count, new.last_accessed) == (
        "2026-09-02T03:37:46+00:00", "2026-09-02T08:02:42+00:00", "working", 4, "2026-09-20T10:00:00+00:00")
    assert new.updated_at > "2026-10-05"  # the delta sync must export the corrected row
    assert store.recompute_tiers() is not None
    assert store.get_claim(new.id, include_citations=False).tier != "core"


def test_retired_claims_and_same_scope_moves_are_refused(store) -> None:
    claim_id = _claim(store, "confirmed")
    with pytest.raises(ValueError, match="already"):
        rescope.rescope_claim(store, claim_id, "project:py-apps", reason="noop")
    lifecycle.transition_claim(store, claim_id, "archived", reason="fixture", event_type="compactor")
    with pytest.raises(ValueError, match="archived"):
        rescope.rescope_claim(store, claim_id, "project:whatsappbot", reason="retired")


def _confirmed(store, scope: str, object_value: str) -> int:
    claim = store.create_claim(text=f"service url is {object_value}", subject="service", predicate="url",
                               object_value=object_value, citations=[CitationInput(source="session://y")],
                               scope=scope)
    lifecycle.transition_claim(store, claim.id, "confirmed", reason="fixture", event_type="validator")
    return claim.id


def test_a_move_onto_an_identical_confirmed_claim_supersedes_by_it(store) -> None:
    # Found 2026-10-06 unifying 'project:py apps' into 'project:py-apps': the target already
    # held the same confirmed (subject, predicate), the copy hit the unique index and a
    # half-made candidate was left behind.
    keeper = _confirmed(store, "project:py-apps", "http://localhost:3004/send")
    old = _confirmed(store, "project:py apps", "http://localhost:3004/send")
    before = store.count_claims() if hasattr(store, "count_claims") else None
    assert rescope.rescope_claim(store, old, "project:py-apps", reason="spelling") == keeper
    assert store.get_claim(old, include_citations=False).replaced_by_claim_id == keeper
    if before is not None:
        assert store.count_claims() == before


def test_a_move_onto_a_different_confirmed_value_is_refused_without_side_effects(store) -> None:
    _confirmed(store, "project:py-apps", "http://localhost:3004/send")
    old = _confirmed(store, "project:py apps", "http://localhost:9999/send")
    with pytest.raises(rescope.RescopeConflict):
        rescope.rescope_claim(store, old, "project:py-apps", reason="spelling")
    assert store.get_claim(old, include_citations=False).status == "confirmed"
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM claims WHERE scope = 'project:py-apps'").fetchone()[0] == 1


def test_a_move_that_fails_midway_leaves_no_live_orphan(store, monkeypatch) -> None:
    # Found 2026-10-06: a Dreaming-sourced claim cannot be re-confirmed without a source
    # review, so the copy failed after creation and stayed as a live candidate.
    old_id = _claim(store, "confirmed")
    real = rescope.lifecycle.transition_claim

    def refuse_copy(st, claim_id, to_status, **kwargs):
        if claim_id != old_id and to_status == "confirmed":
            raise ValueError("Dreaming confirmation requires a current source review")
        return real(st, claim_id, to_status, **kwargs)

    monkeypatch.setattr(rescope.lifecycle, "transition_claim", refuse_copy)
    with pytest.raises(rescope.RescopeConflict, match="source review"):
        rescope.rescope_claim(store, old_id, "project:whatsappbot", reason="midway")
    assert store.get_claim(old_id, include_citations=False).status == "confirmed"
    with store.connect() as conn:
        live = conn.execute("SELECT COUNT(*) FROM claims WHERE scope = 'project:whatsappbot' "
                            "AND status != 'archived'").fetchone()[0]
    assert live == 0


def test_a_claim_the_ingest_filter_now_refuses_stays_where_it_is(store, monkeypatch) -> None:
    # Found 2026-10-06: an old claim whose citation locator holds a home path cannot be
    # re-ingested (the sensitivity filter is right); the batch must skip it, not stop.
    from memorymaster.core.security import SensitiveMetadataError

    old_id = _claim(store, "confirmed")

    def refuse(**_kwargs):
        raise SensitiveMetadataError("citation_locator", ["home_path_unix"])

    monkeypatch.setattr(store, "create_claim", refuse)
    with pytest.raises(rescope.RescopeConflict, match="citation_locator"):
        rescope.rescope_claim(store, old_id, "project:whatsappbot", reason="filter")
    assert store.get_claim(old_id, include_citations=False).status == "confirmed"
