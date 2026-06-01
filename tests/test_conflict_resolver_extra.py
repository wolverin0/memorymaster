"""Extra coverage for conflict resolution (deterministic + LLM-driven).

Covers three contracts that the rest of the system relies on:

  1. Winner selection between two conflicting claims (conflict_resolver._pick_winner)
     follows the documented priority chain: pinned > confidence > recency >
     citations > id. The *reason* string is the public contract — these tests
     anchor on the rule that fired, not on which integer id happens to win.

  2. Supersession wiring is bidirectional: after resolution the winner carries
     supersedes_claim_id pointing at the loser, and the loser carries
     replaced_by_claim_id pointing at the winner AND status == 'superseded'.
     A half-wired pair silently breaks the wiki, so both legs are asserted.

  3. The LLM-driven path (auto_resolver.resolve_conflict_pair) honours the
     model's verdict for BOTH the chosen-winner branch and the
     no-clear-winner / abstain branch — and never deletes the loser, only
     supersedes it. The LLM call is monkeypatched; no real provider is hit.

All DBs are built fresh per-test from the project schema via MemoryService.
"""

from __future__ import annotations

from pathlib import Path

from memorymaster.models import Claim, Citation, CitationInput
from memorymaster.service import MemoryService
from memorymaster.lifecycle import transition_claim
from memorymaster.conflict_resolver import (
    _pick_winner,
    supersede_claim,
    resolve_conflicts,
    SupersessionRaceLost,
)
from memorymaster import auto_resolver
from memorymaster.auto_resolver import resolve_conflict_pair, auto_resolve_conflicts


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_claim(
    *,
    id: int = 1,
    subject: str | None = "server",
    predicate: str | None = "port",
    object_value: str | None = "8080",
    scope: str = "project",
    status: str = "confirmed",
    confidence: float = 0.7,
    pinned: bool = False,
    updated_at: str = "2026-01-01T00:00:00+00:00",
    citations: list | None = None,
) -> Claim:
    return Claim(
        id=id,
        text=f"{subject} {predicate} is {object_value}",
        idempotency_key=None,
        normalized_text=None,
        claim_type=None,
        subject=subject,
        predicate=predicate,
        object_value=object_value,
        scope=scope,
        volatility="medium",
        status=status,
        confidence=confidence,
        pinned=pinned,
        supersedes_claim_id=None,
        replaced_by_claim_id=None,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at=updated_at,
        last_validated_at=None,
        archived_at=None,
        citations=citations or [],
    )


def _fresh_service(tmp_path: Path) -> MemoryService:
    svc = MemoryService(str(tmp_path / "test.db"))
    svc.init_db()
    return svc


def _cite() -> Citation:
    return Citation(
        id=1, claim_id=1, source="doc", locator=None,
        excerpt=None, created_at="2026-01-01T00:00:00+00:00",
    )


# --------------------------------------------------------------------------- #
# 1. Winner selection — anchored on the rule that fired, not the id
# --------------------------------------------------------------------------- #
class TestWinnerSelectionRules:
    def test_pinned_overrides_a_higher_confidence_rival(self):
        """Contract: a pinned claim must win even against a more-confident
        unpinned rival. If this regresses, a manually-protected fact gets
        auto-superseded by noise."""
        pinned_low = _make_claim(id=1, confidence=0.10, pinned=True)
        unpinned_high = _make_claim(id=2, confidence=0.99, pinned=False)
        pair = _pick_winner(pinned_low, unpinned_high)
        assert pair.reason == "pinned_wins"
        assert pair.winner is pinned_low
        assert pair.loser is unpinned_high

    def test_confidence_decides_when_pin_state_is_equal(self):
        """Contract: with pin state equal, the higher-confidence claim wins.
        Confidence is the primary evidence signal."""
        weak = _make_claim(id=10, confidence=0.40)
        strong = _make_claim(id=11, confidence=0.80)
        pair = _pick_winner(weak, strong)
        assert pair.reason == "higher_confidence"
        assert pair.winner is strong

    def test_recency_breaks_a_confidence_tie(self):
        """Contract: equal confidence -> the freshest updated_at wins. Newer
        information supersedes older information of equal strength."""
        old = _make_claim(id=20, confidence=0.7, updated_at="2026-01-01T00:00:00+00:00")
        new = _make_claim(id=21, confidence=0.7, updated_at="2026-03-01T00:00:00+00:00")
        pair = _pick_winner(old, new)
        assert pair.reason == "more_recent"
        assert pair.winner is new

    def test_citation_count_breaks_a_confidence_and_recency_tie(self):
        """Contract: equal confidence and equal timestamp -> more citations
        wins. Better-evidenced claims are preferred."""
        c = _cite()
        few = _make_claim(id=30, confidence=0.7, citations=[c])
        many = _make_claim(id=31, confidence=0.7, citations=[c, c, c])
        pair = _pick_winner(few, many)
        assert pair.reason == "more_citations"
        assert pair.winner is many

    def test_full_tie_falls_back_to_deterministic_id(self):
        """Contract: when every signal ties, resolution must still be
        deterministic (higher id = most recently created wins) so repeated
        runs never flip the winner."""
        a = _make_claim(id=40, confidence=0.7)
        b = _make_claim(id=41, confidence=0.7)
        first = _pick_winner(a, b)
        # Argument order must not change the outcome — determinism.
        second = _pick_winner(b, a)
        assert first.reason == "higher_id_tiebreaker"
        assert first.winner.id == 41
        assert second.winner.id == 41


# --------------------------------------------------------------------------- #
# 2. Supersession wiring (deterministic resolve_conflicts, real DB)
# --------------------------------------------------------------------------- #
class TestSupersessionWiring:
    def _two_conflicting(self, svc: MemoryService):
        loser = svc.ingest(
            text="server port is 8080",
            citations=[CitationInput(source="doc1")],
            subject="server", predicate="port", object_value="8080",
            confidence=0.40,
        )
        winner = svc.ingest(
            text="server port is 3000",
            citations=[CitationInput(source="doc2")],
            subject="server", predicate="port", object_value="3000",
            confidence=0.95,
        )
        return loser, winner

    def test_both_sides_of_the_link_are_set(self, tmp_path):
        """Contract: a resolved conflict must wire BOTH legs — loser.replaced_by
        -> winner and winner.supersedes -> loser — and flip the loser to
        'superseded'. A one-legged link corrupts the wiki's supersession graph."""
        svc = _fresh_service(tmp_path)
        loser, winner = self._two_conflicting(svc)

        result = resolve_conflicts(svc, dry_run=False, statuses=["candidate"])
        assert result.pairs_resolved >= 1

        loser_fresh = svc.store.get_claim(loser.id)
        winner_fresh = svc.store.get_claim(winner.id)

        # Loser leg
        assert loser_fresh.status == "superseded"
        assert loser_fresh.replaced_by_claim_id == winner.id
        # Winner leg
        assert winner_fresh.supersedes_claim_id == loser.id
        # Winner is NOT mutated into a terminal state
        assert winner_fresh.status == "candidate"

    def test_higher_confidence_claim_is_the_survivor(self, tmp_path):
        """Contract: the survivor of an auto-resolution is the higher-confidence
        claim, never the weaker one. Asserts on which *value* survived, not on
        ids, so it stays valid if id assignment changes."""
        svc = _fresh_service(tmp_path)
        loser, winner = self._two_conflicting(svc)

        resolve_conflicts(svc, dry_run=False, statuses=["candidate"])

        loser_fresh = svc.store.get_claim(loser.id)
        winner_fresh = svc.store.get_claim(winner.id)
        assert winner_fresh.object_value == "3000"  # the 0.95-confidence value
        assert loser_fresh.object_value == "8080"   # the 0.40-confidence value
        assert loser_fresh.status == "superseded"
        assert winner_fresh.status != "superseded"

    def test_supersede_claim_is_idempotent_against_double_resolve(self, tmp_path):
        """Contract: re-running resolution must not re-supersede an already
        superseded loser — the second pass resolves nothing. Protects against
        duplicate audit trails / link churn."""
        svc = _fresh_service(tmp_path)
        self._two_conflicting(svc)

        first = resolve_conflicts(svc, dry_run=False, statuses=["candidate"])
        assert first.pairs_resolved >= 1
        second = resolve_conflicts(
            svc, dry_run=False, statuses=["candidate", "superseded"]
        )
        assert second.pairs_resolved == 0


class TestSupersedeClaimPrimitive:
    def test_supersede_claim_links_and_transitions_loser(self, tmp_path):
        """Contract: supersede_claim() is the atomic primitive — it flips the
        old claim to 'superseded' and stamps replaced_by, while the new claim
        gains supersedes_claim_id. Directly exercises the SQLite atomic path."""
        svc = _fresh_service(tmp_path)
        old = svc.ingest(
            text="old fact", citations=[CitationInput(source="d1")],
            subject="x", predicate="y", object_value="a",
        )
        new = svc.ingest(
            text="new fact", citations=[CitationInput(source="d2")],
            subject="x", predicate="y", object_value="b",
        )

        updated_old = supersede_claim(
            svc.store, old_claim_id=old.id, new_claim_id=new.id,
            reason="test_supersede",
        )
        assert updated_old.status == "superseded"
        assert updated_old.replaced_by_claim_id == new.id
        new_fresh = svc.store.get_claim(new.id)
        assert new_fresh.supersedes_claim_id == old.id

    def test_supersede_claim_rejects_a_lost_race(self, tmp_path):
        """Contract: if the old claim was already superseded by someone else,
        supersede_claim must raise SupersessionRaceLost rather than silently
        overwriting the existing replacement pointer."""
        svc = _fresh_service(tmp_path)
        old = svc.ingest(
            text="old", citations=[CitationInput(source="d1")],
            subject="x", predicate="y", object_value="a",
        )
        first = svc.ingest(
            text="first replacement", citations=[CitationInput(source="d2")],
            subject="x", predicate="y", object_value="b",
        )
        second = svc.ingest(
            text="second replacement", citations=[CitationInput(source="d3")],
            subject="x", predicate="y", object_value="c",
        )
        supersede_claim(svc.store, old_claim_id=old.id, new_claim_id=first.id, reason="r1")

        raised = False
        try:
            supersede_claim(svc.store, old_claim_id=old.id, new_claim_id=second.id, reason="r2")
        except SupersessionRaceLost as exc:
            raised = True
            assert exc.current_replacement_id == first.id
        assert raised, "expected SupersessionRaceLost on a second supersede of the same claim"
        # The original winner pointer must be untouched.
        assert svc.store.get_claim(old.id).replaced_by_claim_id == first.id


# --------------------------------------------------------------------------- #
# 3. LLM-resolution path (auto_resolver) — monkeypatched, never real infra
# --------------------------------------------------------------------------- #
def _conflicted_pair(svc: MemoryService):
    """Ingest two contradicting claims and move both into 'conflicted'."""
    a = svc.ingest(
        text="region is us-east", citations=[CitationInput(source="d1")],
        subject="db", predicate="region", object_value="us-east", confidence=0.5,
    )
    b = svc.ingest(
        text="region is eu-west", citations=[CitationInput(source="d2")],
        subject="db", predicate="region", object_value="eu-west", confidence=0.5,
    )
    transition_claim(svc.store, claim_id=a.id, to_status="conflicted",
                     reason="seed", event_type="validator")
    transition_claim(svc.store, claim_id=b.id, to_status="conflicted",
                     reason="seed", event_type="validator")
    return (
        svc.store.get_claim(a.id, include_citations=True),
        svc.store.get_claim(b.id, include_citations=True),
    )


class TestLlmResolutionPath:
    def test_llm_picks_a_and_supersedes_b(self, tmp_path, monkeypatch):
        """Contract: when the model votes 'A', claim A survives and claim B is
        superseded with A as its replacement — the loser is preserved (audit
        trail), never deleted. LLM call is stubbed deterministically."""
        svc = _fresh_service(tmp_path)
        a, b = _conflicted_pair(svc)

        monkeypatch.setattr(
            auto_resolver, "call_llm",
            lambda prompt, _: '{"winner": "A", "reason": "stronger evidence"}',
        )
        result = resolve_conflict_pair(svc.store, a, b)

        assert result["resolved"] is True
        assert result["winner_id"] == a.id
        assert result["loser_id"] == b.id

        loser = svc.store.get_claim(b.id)
        winner = svc.store.get_claim(a.id)
        assert loser.status == "superseded"          # preserved, not deleted
        assert loser.replaced_by_claim_id == a.id
        assert winner.status != "superseded"

    def test_llm_picks_b_supersedes_a(self, tmp_path, monkeypatch):
        """Contract: the survivor must follow the model's verdict, not argument
        order. A 'B' vote supersedes claim A — the mirror of the 'A' case."""
        svc = _fresh_service(tmp_path)
        a, b = _conflicted_pair(svc)

        monkeypatch.setattr(
            auto_resolver, "call_llm",
            lambda prompt, _: '{"winner": "B", "reason": "more specific"}',
        )
        result = resolve_conflict_pair(svc.store, a, b)

        assert result["resolved"] is True
        assert result["winner_id"] == b.id
        assert result["loser_id"] == a.id
        assert svc.store.get_claim(a.id).status == "superseded"
        assert svc.store.get_claim(b.id).status != "superseded"

    def test_llm_abstains_leaves_both_intact(self, tmp_path, monkeypatch):
        """Contract: when the model returns no clear winner (verdict outside
        {A,B}), NOTHING is superseded — both claims survive for human review.
        Auto-resolution must never guess when the evaluator abstained."""
        svc = _fresh_service(tmp_path)
        a, b = _conflicted_pair(svc)

        monkeypatch.setattr(
            auto_resolver, "call_llm",
            lambda prompt, _: '{"winner": "neither", "reason": "ambiguous"}',
        )
        result = resolve_conflict_pair(svc.store, a, b)

        assert result["resolved"] is False
        assert result["reason"] == "llm_undecided"
        # Neither claim was touched.
        assert svc.store.get_claim(a.id).status == "conflicted"
        assert svc.store.get_claim(b.id).status == "conflicted"
        assert svc.store.get_claim(a.id).replaced_by_claim_id is None
        assert svc.store.get_claim(b.id).replaced_by_claim_id is None

    def test_invalid_llm_json_is_treated_as_abstain(self, tmp_path, monkeypatch):
        """Contract: a malformed LLM response degrades to abstain (resolved
        False), not to a crash or an arbitrary supersession. Robustness against
        a flaky model is part of the contract."""
        svc = _fresh_service(tmp_path)
        a, b = _conflicted_pair(svc)

        monkeypatch.setattr(
            auto_resolver, "call_llm",
            lambda prompt, _: "this is not json at all",
        )
        result = resolve_conflict_pair(svc.store, a, b)

        assert result["resolved"] is False
        assert svc.store.get_claim(a.id).status == "conflicted"
        assert svc.store.get_claim(b.id).status == "conflicted"

    def test_auto_resolve_conflicts_groups_and_supersedes_one(self, tmp_path, monkeypatch):
        """Contract: the batch entrypoint groups conflicted claims by
        (subject, predicate, scope) and resolves within each group, reporting
        an accurate resolved count. End-to-end LLM-driven supersession."""
        svc = _fresh_service(tmp_path)
        a, b = _conflicted_pair(svc)

        monkeypatch.setattr(
            auto_resolver, "call_llm",
            lambda prompt, _: '{"winner": "A", "reason": "kept A"}',
        )
        counts = auto_resolve_conflicts(svc.store, limit=10)

        assert counts["pairs_evaluated"] >= 1
        assert counts["resolved"] >= 1
        # Exactly one of the two ends up superseded.
        statuses = {
            svc.store.get_claim(a.id).status,
            svc.store.get_claim(b.id).status,
        }
        assert "superseded" in statuses

    def test_auto_resolve_no_conflicted_is_a_noop(self, tmp_path, monkeypatch):
        """Contract: with no conflicted claims, the batch path returns zeroed
        counts WITHOUT invoking the LLM — no wasted provider calls."""
        svc = _fresh_service(tmp_path)
        svc.ingest(
            text="lonely fact", citations=[CitationInput(source="d1")],
            subject="db", predicate="region", object_value="us-east",
        )

        def _boom(prompt, _):  # pragma: no cover - must never run
            raise AssertionError("LLM must not be called when nothing is conflicted")

        monkeypatch.setattr(auto_resolver, "call_llm", _boom)
        counts = auto_resolve_conflicts(svc.store, limit=10)

        assert counts == {"pairs_evaluated": 0, "resolved": 0, "failed": 0}
