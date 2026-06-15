from __future__ import annotations

from pathlib import Path

from memorymaster.core.lifecycle import transition_claim
from memorymaster.core.models import CitationInput
from memorymaster.stores.storage import SQLiteStore


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "memorymaster-extra.db")
    store.init_db()
    return store


def _cite(label: str = "evidence") -> CitationInput:
    return CitationInput(source="test://storage-extra", locator=label, excerpt=f"{label} excerpt")


def test_status_transition_records_audit_event_and_temporal_fields(tmp_path: Path) -> None:
    """Lifecycle decisions must leave enough state and event history for later audits."""
    store = _store(tmp_path)
    claim = store.create_claim(
        "candidate storage transition claim",
        [_cite("transition")],
        subject="storage-transition",
        predicate="status",
        object_value="candidate",
    )

    confirmed = transition_claim(
        store,
        claim.id,
        "confirmed",
        "validator accepted storage evidence",
        event_type="validator",
    )

    assert confirmed.status == "confirmed"
    assert confirmed.version == claim.version + 1
    assert confirmed.last_validated_at is not None
    assert confirmed.archived_at is None

    events = store.list_events(claim_id=claim.id, event_type="validator")
    assert len(events) == 1
    assert events[0].from_status == "candidate"
    assert events[0].to_status == "confirmed"
    assert events[0].details == "validator accepted storage evidence"


def test_fts_index_tracks_normalized_text_replacements(tmp_path: Path) -> None:
    """Recall search depends on FTS triggers removing stale normalized text tokens."""
    store = _store(tmp_path)
    claim = store.create_claim("ordinary storage claim", [_cite("fts")])

    store.set_normalized_text(claim.id, "normalized-alpha-token")
    alpha_hits = store.list_claims(text_query="normalized-alpha-token")
    assert [hit.id for hit in alpha_hits] == [claim.id]

    store.set_normalized_text(claim.id, "normalized-beta-token")

    assert store.list_claims(text_query="normalized-alpha-token") == []
    beta_hits = store.list_claims(text_query="normalized-beta-token")
    assert [hit.id for hit in beta_hits] == [claim.id]


def test_citations_round_trip_through_single_and_batch_reads(tmp_path: Path) -> None:
    """Evidence provenance must survive both direct reads and list batching."""
    store = _store(tmp_path)
    first = store.create_claim(
        "claim with two ordered citations",
        [
            CitationInput(source="test://source-a", locator="line-1", excerpt="first excerpt"),
            {"source": "test://source-b", "locator": "line-2", "excerpt": "second excerpt"},
        ],
    )
    second = store.create_claim("claim with one citation", [_cite("line-3")])

    direct = store.list_citations(first.id)
    assert [(citation.source, citation.locator) for citation in direct] == [
        ("test://source-a", "line-1"),
        ("test://source-b", "line-2"),
    ]

    batched = store.list_citations_batch([first.id, second.id])
    assert [citation.excerpt for citation in batched[first.id]] == ["first excerpt", "second excerpt"]
    assert [citation.locator for citation in batched[second.id]] == ["line-3"]

    listed = store.list_claims(include_citations=True, limit=10)
    by_id = {claim.id: claim for claim in listed}
    assert [citation.source for citation in by_id[first.id].citations] == [
        "test://source-a",
        "test://source-b",
    ]


def test_query_as_of_respects_validity_window_and_archived_exclusion(tmp_path: Path) -> None:
    """Time-travel reads must return only claims that were valid at the requested instant."""
    store = _store(tmp_path)
    current = store.create_claim(
        "policy was active in January",
        [_cite("jan-policy")],
        valid_from="2026-01-01T00:00:00+00:00",
        valid_until="2026-02-01T00:00:00+00:00",
    )
    future = store.create_claim(
        "policy starts in March",
        [_cite("mar-policy")],
        valid_from="2026-03-01T00:00:00+00:00",
    )
    archived = store.create_claim(
        "archived claim should not be returned",
        [_cite("archived")],
        valid_from="2026-01-01T00:00:00+00:00",
    )
    transition_claim(store, archived.id, "archived", "not current", event_type="transition")

    jan_hits = store.query_as_of("2026-01-15T00:00:00+00:00")
    assert [claim.id for claim in jan_hits] == [current.id]
    assert [citation.locator for citation in jan_hits[0].citations] == ["jan-policy"]

    boundary_hits = store.query_as_of("2026-02-01T00:00:00+00:00")
    assert current.id not in {claim.id for claim in boundary_hits}
    assert future.id not in {claim.id for claim in boundary_hits}


def test_idempotency_key_lookup_reuses_existing_claim_and_citations(tmp_path: Path) -> None:
    """Retried ingestion must be exactly-once and keep the original evidence packet."""
    store = _store(tmp_path)
    original = store.create_claim(
        "original idempotent text",
        [_cite("first-write")],
        idempotency_key="  storage-extra-key  ",
        subject="idempotency",
        predicate="preserves",
        object_value="original",
    )

    retried = store.create_claim(
        "different text from retry",
        [_cite("retry-write")],
        idempotency_key="storage-extra-key",
        subject="idempotency",
        predicate="preserves",
        object_value="retry",
    )
    looked_up = store.get_claim_by_idempotency_key(" storage-extra-key ")

    assert retried.id == original.id
    assert retried.text == "original idempotent text"
    assert looked_up is not None
    assert looked_up.id == original.id
    assert [citation.locator for citation in looked_up.citations] == ["first-write"]
    assert store.get_claim_by_idempotency_key("   ") is None
