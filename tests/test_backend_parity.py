"""SQLite/Postgres parity gate (v3.20.0-S2).

Unlike test_postgres_parity.py (which verifies each PostgresStore method works
in isolation), these tests run the SAME scenario against BOTH backends via the
`parametrize_backends` fixture and assert each produces the SAME observable
result against a fixed golden expectation. If SQLite and Postgres both satisfy
the identical assertions, they are at parity.

SQLite always runs. Postgres runs only when MEMORYMASTER_TEST_POSTGRES_DSN is
set (the `postgres` param is marked and skips otherwise), so dev machines
without a Postgres stay green while CI exercises both.
"""
from __future__ import annotations

from memorymaster.core.models import CitationInput


def _ingest(svc, text, **kw):
    return svc.ingest(
        text=text,
        citations=[CitationInput(source="parity://src", locator="loc", excerpt="exc")],
        source_agent="parity-test",
        **kw,
    )


# ---------------------------------------------------------------------------
# Ingest + list
# ---------------------------------------------------------------------------


def test_parity_ingest_then_list(parametrize_backends):
    backend, svc = parametrize_backends
    _ingest(svc, "parity claim alpha")
    _ingest(svc, "parity claim beta")
    _ingest(svc, "parity claim gamma")

    claims = svc.store.list_claims(status="candidate")
    texts = sorted(c.text for c in claims)
    assert texts == [
        "parity claim alpha",
        "parity claim beta",
        "parity claim gamma",
    ], f"{backend}: unexpected claim set {texts}"


# ---------------------------------------------------------------------------
# Status transition
# ---------------------------------------------------------------------------


def test_parity_status_transition(parametrize_backends):
    backend, svc = parametrize_backends
    cid = _ingest(svc, "parity transition claim").id
    claim = svc.store.get_claim(cid)
    assert claim.status == "candidate", f"{backend}: fresh claim should be candidate"

    updated = svc.store.apply_status_transition(
        claim, to_status="confirmed", reason="parity", event_type="validator"
    )
    assert updated.status == "confirmed", f"{backend}: transition to confirmed failed"

    refetched = svc.store.get_claim(cid)
    assert refetched.status == "confirmed", f"{backend}: status not persisted"


# ---------------------------------------------------------------------------
# Citations round-trip
# ---------------------------------------------------------------------------


def test_parity_citations_round_trip(parametrize_backends):
    backend, svc = parametrize_backends
    claim = svc.ingest(
        text="parity multi-citation claim",
        citations=[
            CitationInput(source="parity://a", locator="l1", excerpt="e1"),
            CitationInput(source="parity://b", locator="l2", excerpt="e2"),
        ],
        source_agent="parity-test",
    )
    cites = svc.store.list_citations(claim.id)
    sources = sorted(c.source for c in cites)
    assert sources == ["parity://a", "parity://b"], f"{backend}: citations {sources}"
    assert svc.store.count_citations(claim.id) == 2, f"{backend}: count mismatch"


# ---------------------------------------------------------------------------
# Events written on mutation
# ---------------------------------------------------------------------------


def test_parity_ingest_emits_event(parametrize_backends):
    backend, svc = parametrize_backends
    cid = _ingest(svc, "parity event claim").id
    events = svc.store.list_events(claim_id=cid)
    event_types = {e.event_type for e in events}
    assert "ingest" in event_types, f"{backend}: no ingest event, got {event_types}"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_parity_idempotency_key(parametrize_backends):
    backend, svc = parametrize_backends
    c1 = svc.store.create_claim(
        "parity idem first",
        [CitationInput(source="s", locator="l")],
        idempotency_key="parity-idem-1",
        source_agent=getattr(svc, "principal", None),
    )
    c2 = svc.store.create_claim(
        "parity idem second different text",
        [CitationInput(source="s", locator="l")],
        idempotency_key="parity-idem-1",
        source_agent=getattr(svc, "principal", None),
    )
    assert c1.id == c2.id, f"{backend}: idempotency_key did not dedup"


# ---------------------------------------------------------------------------
# Retrieval rank order on a fixed corpus
# ---------------------------------------------------------------------------


def test_parity_retrieval_top_hit(parametrize_backends):
    backend, svc = parametrize_backends
    _ingest(svc, "the deployment pipeline uses GitHub Actions for CI")
    _ingest(svc, "the database is PostgreSQL with WAL mode enabled")
    _ingest(svc, "the frontend is built with React and Vite")

    rows = svc.query_rows(
        query_text="continuous integration pipeline",
        limit=3,
        include_candidates=True,
        retrieval_mode="hybrid",
        allow_sensitive=True,
    )
    assert rows, f"{backend}: query returned no rows"
    top_text = rows[0]["claim"].text
    assert "GitHub Actions" in top_text, (
        f"{backend}: expected CI claim as top hit, got {top_text!r}"
    )


# ---------------------------------------------------------------------------
# Status-filtered list parity
# ---------------------------------------------------------------------------


def test_parity_status_filter_excludes_other_states(parametrize_backends):
    backend, svc = parametrize_backends
    keep = _ingest(svc, "parity confirmed-only claim").id
    _ingest(svc, "parity still-candidate claim")
    claim = svc.store.get_claim(keep)
    svc.store.apply_status_transition(
        claim, to_status="confirmed", reason="parity", event_type="validator"
    )

    confirmed = svc.store.list_claims(status="confirmed")
    confirmed_texts = [c.text for c in confirmed]
    assert "parity confirmed-only claim" in confirmed_texts, f"{backend}: missing confirmed"
    assert "parity still-candidate claim" not in confirmed_texts, (
        f"{backend}: candidate leaked into confirmed filter"
    )
