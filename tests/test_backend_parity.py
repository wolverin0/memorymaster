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


# ---------------------------------------------------------------------------
# R6 — writes that only ever failed on Postgres
#
# These are the parity assertions the silent-dropper class defeated: each one
# passed on SQLite and failed invisibly on Postgres because the inherited
# `?`-placeholder SQL was rejected and the caller suppressed the exception.
# ---------------------------------------------------------------------------


def test_parity_recall_records_access_count(parametrize_backends):
    backend, svc = parametrize_backends
    claim = _ingest(svc, "parity access counter claim")

    svc._record_accesses([{"claim": claim}], query_text="access counter")

    reloaded = svc.store.get_claim(claim.id)
    assert reloaded.access_count == 1, (
        f"{backend}: access_count stayed {reloaded.access_count} — "
        "recompute_tiers will read this as 'never accessed'"
    )
    assert reloaded.last_accessed, f"{backend}: last_accessed was not stamped"


def test_parity_batched_recall_records_every_claim(parametrize_backends):
    backend, svc = parametrize_backends
    first = _ingest(svc, "parity batch access alpha")
    second = _ingest(svc, "parity batch access beta")

    svc.store.record_accesses_batch([first.id, second.id])

    counts = [svc.store.get_claim(cid).access_count for cid in (first.id, second.id)]
    assert counts == [1, 1], f"{backend}: batched access write dropped rows {counts}"


def _claim_entity_id(store, claim_id):
    """Read ``claims.entity_id`` in whichever dialect the store speaks."""
    if type(store).__name__ == "PostgresStore":
        with store.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT entity_id FROM claims WHERE id = %s", (claim_id,))
            row = cur.fetchone()
            return None if row is None else row["entity_id"]
    with store.connect() as conn:
        row = conn.execute(
            "SELECT entity_id FROM claims WHERE id = ?", (claim_id,)
        ).fetchone()
        return None if row is None else row[0]


def test_parity_ingest_links_claim_to_canonical_entity(parametrize_backends):
    backend, svc = parametrize_backends
    claim = _ingest(svc, "Qdrant is the vector index", subject="Qdrant")

    assert _claim_entity_id(svc.store, claim.id), (
        f"{backend}: claim was not linked to a canonical entity"
    )


def test_parity_entity_registry_resolves_on_both_backends(parametrize_backends):
    from memorymaster.knowledge.entity_registry import add_alias, resolve_or_create

    backend, svc = parametrize_backends
    with svc.store.connect() as conn:
        entity_id = resolve_or_create(
            conn, "MemoryMaster", entity_type="project", scope="project:mm"
        )
        assert entity_id > 0, f"{backend}: entity was not created"
        assert add_alias(conn, entity_id, "mm") is True, f"{backend}: alias not written"
        assert add_alias(conn, entity_id, "mm") is False, f"{backend}: alias not deduped"
        same = resolve_or_create(conn, "MemoryMaster", scope="project:mm")
        assert same == entity_id, f"{backend}: alias lookup did not resolve"
        conn.commit()
