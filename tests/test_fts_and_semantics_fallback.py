"""One out-of-vocabulary word must not zero out an otherwise-matching query.

FTS5 joins tokens with implicit AND (``_escape_fts5_query``). A natural-language
question routinely carries a word that appears nowhere in the corpus — "purpose",
"why", "explain" — and under strict AND that single word drops a perfect match to
zero rows. The caller cannot distinguish "nothing is stored" from "one word was
unlucky", so an agent concludes it has no memory on the subject and proceeds
without it.

The fix relaxes to OR over the in-vocabulary terms *only when the strict query
returns nothing*, and reports that it did so. These tests pin both halves: the
relaxation happens, and it stays off when the strict query already answered.
"""
from __future__ import annotations

import pytest

from memorymaster.core.models import CitationInput


@pytest.fixture()
def store_with_claim(tmp_path):
    from memorymaster.core.service import MemoryService

    svc = MemoryService(tmp_path / "mm.db", workspace_root=tmp_path)
    svc.init_db()
    svc.ingest(
        text="The quokka bridge resolves telemetry handles for the ingest pipeline.",
        citations=[CitationInput(source="test", locator="l1", excerpt="e1")],
        scope="project:test",
        source_agent="pytest",
    )
    return svc


def _texts(claims):
    return [c.text for c in claims]


def test_out_of_vocabulary_word_does_not_zero_the_query(store_with_claim):
    """'quokka' matches; 'purpose' is absent. Strict AND returns nothing."""
    svc = store_with_claim
    strict = svc.store.list_claims(text_query="quokka", limit=10, status_in=["candidate"])
    assert len(strict) == 1, "precondition: the in-vocabulary term alone must match"

    relaxed = svc.store.list_claims(
        text_query="quokka purpose", limit=10, status_in=["candidate"]
    )
    assert len(relaxed) == 1, (
        "a single out-of-vocabulary word ('purpose') zeroed an otherwise-perfect "
        "match — this is the bug"
    )
    assert "quokka" in _texts(relaxed)[0]


def test_relaxation_is_reported_to_the_caller(store_with_claim):
    """A relaxed result must be distinguishable from a strict one."""
    svc = store_with_claim

    strict_info: dict = {}
    svc.store.list_claims(
        text_query="quokka", limit=10, status_in=["candidate"], relaxed_out=strict_info
    )
    assert strict_info.get("relaxed") is not True, "strict hit must not claim relaxation"

    relaxed_info: dict = {}
    svc.store.list_claims(
        text_query="quokka purpose",
        limit=10,
        status_in=["candidate"],
        relaxed_out=relaxed_info,
    )
    assert relaxed_info.get("relaxed") is True
    assert "purpose" in relaxed_info.get("dropped_terms", [])


def test_all_terms_out_of_vocabulary_still_returns_nothing(store_with_claim):
    """Relaxing must not turn an unrelated query into noise."""
    svc = store_with_claim
    rows = svc.store.list_claims(
        text_query="xyzzy plugh frobnicate", limit=10, status_in=["candidate"]
    )
    assert rows == []


def test_single_out_of_vocabulary_term_returns_nothing(store_with_claim):
    """Nothing to relax to when there is only one term and it does not match."""
    svc = store_with_claim
    assert svc.store.list_claims(text_query="xyzzy", limit=10, status_in=["candidate"]) == []


def test_strict_hits_rank_ahead_of_relaxed_ones(store_with_claim):
    """Relaxed rows supplement the strict ones; they never displace them.

    Substituting instead of supplementing made the relaxation depend on the
    whole corpus vocabulary: a claim the caller filters out of its own output
    could still enter the index, satisfy the strict AND, and thereby suppress
    the relaxation that was carrying real results. Appending keeps the strict
    answer first and makes the outcome independent of unrelated claims.
    """
    svc = store_with_claim
    svc.ingest(
        text="An unrelated claim mentioning telemetry only.",
        citations=[CitationInput(source="test", locator="l2", excerpt="e2")],
        scope="project:test",
        source_agent="pytest",
    )
    rows = svc.store.list_claims(
        text_query="quokka telemetry", limit=10, status_in=["candidate"]
    )
    assert "quokka" in rows[0].text, "the strict AND match must rank first"


def test_a_later_unrelated_claim_does_not_suppress_results(store_with_claim):
    """The regression that substituting caused, pinned directly."""
    svc = store_with_claim
    before = svc.store.list_claims(
        text_query="quokka dependency", limit=10, status_in=["candidate"]
    )
    assert before, "precondition: 'dependency' is absent, so relaxation carries the result"

    # Teach the index the missing word via a claim that is not the answer.
    svc.ingest(
        text="Quokka dependency bookkeeping for an unrelated subsystem.",
        citations=[CitationInput(source="test", locator="l3", excerpt="e3")],
        scope="project:other",
        source_agent="pytest",
    )
    after = svc.store.list_claims(
        text_query="quokka dependency",
        limit=10,
        status_in=["candidate"],
        scope_allowlist=["project:test"],
    )
    assert after, "a claim outside the caller's scope must not empty the result"


def test_widening_ignores_words_the_ranker_discards(store_with_claim):
    """Common words must not be used to widen the search.

    FTS5 matches "what", "should" and "a" across most of the corpus while the
    ranker discards them as noise and scores every such hit at zero relevance.
    Widening on them therefore buries the real answer under arbitrary claims.
    Both layers must agree on what counts as a term.
    """
    svc = store_with_claim
    svc.ingest(
        text="A note about what a system should do when things follow an order.",
        citations=[CitationInput(source="test", locator="l9", excerpt="e9")],
        scope="project:test",
        source_agent="pytest",
    )
    rows = svc.store.list_claims(
        text_query="what should a quokka do", limit=10, status_in=["candidate"]
    )
    assert rows, "the salient term 'quokka' should still carry the query"
    assert all("quokka" in c.text.lower() for c in rows), (
        "a claim matching only the common words was pulled in as if relevant"
    )


def test_query_of_only_common_words_is_not_widened(store_with_claim):
    """Fewer than two salient terms means the strict verdict stands."""
    svc = store_with_claim
    assert (
        svc.store.list_claims(
            text_query="what should a", limit=10, status_in=["candidate"]
        )
        == []
    )


def test_end_to_end_query_surfaces_the_claim(store_with_claim):
    """The user-visible path, not just the store."""
    svc = store_with_claim
    rows = svc.query_rows(
        "quokka purpose",
        limit=5,
        include_candidates=True,
        scope_allowlist=["project:test"],
        record_accesses=False,
    )
    assert rows, "query_rows returned nothing for a query with one absent word"
    assert any("quokka" in r["claim"].text for r in rows)
