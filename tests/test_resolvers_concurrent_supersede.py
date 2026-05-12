from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from memorymaster.auto_resolver import resolve_conflict_pair
from memorymaster.conflict_resolver import SupersessionRaceLost, supersede_claim
from memorymaster.models import CitationInput
from memorymaster.service import MemoryService


def _fresh_service(tmp_path: Path) -> MemoryService:
    service = MemoryService(str(tmp_path / "race.db"))
    service.init_db()
    return service


def _claim(service: MemoryService, text: str, value: str, confidence: float = 0.5):
    return service.ingest(
        text=text,
        citations=[CitationInput(source="test")],
        subject="resolver",
        predicate="winner",
        object_value=value,
        confidence=confidence,
    )


def _race_supersede(service: MemoryService, old_id: int, replacement_ids: tuple[int, int], monkeypatch):
    barrier = Barrier(2)

    def pick_replacement(_prompt: str):
        barrier.wait(timeout=5)
        return {"winner": "B", "reason": "race regression"}

    monkeypatch.setattr("memorymaster.auto_resolver._llm_evaluate", pick_replacement)

    def auto_supersede(replacement_id: int):
        old = service.store.get_claim(old_id, include_citations=True)
        replacement = service.store.get_claim(replacement_id, include_citations=True)
        return resolve_conflict_pair(service.store, old, replacement)

    def deterministic_supersede(replacement_id: int):
        barrier.wait(timeout=5)
        try:
            supersede_claim(
                service.store,
                old_claim_id=old_id,
                new_claim_id=replacement_id,
                reason="race regression",
            )
        except SupersessionRaceLost as exc:
            return {
                "resolved": False,
                "reason": "lost_race",
                "current_replacement_id": exc.current_replacement_id,
            }
        return {"resolved": True, "winner_id": replacement_id, "loser_id": old_id}

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(auto_supersede, replacement_ids[0]),
            executor.submit(deterministic_supersede, replacement_ids[1]),
        ]
        return [future.result(timeout=10) for future in futures]


def _assert_no_orphan_supersedes(service: MemoryService) -> None:
    with service.store.connect() as conn:
        rows = conn.execute(
            """
            SELECT old.id AS old_id, old.replaced_by_claim_id, new.supersedes_claim_id
            FROM claims old
            JOIN claims new ON new.id = old.replaced_by_claim_id
            WHERE old.replaced_by_claim_id IS NOT NULL
            """
        ).fetchall()
        reverse_rows = conn.execute(
            """
            SELECT new.id AS new_id, new.supersedes_claim_id, old.replaced_by_claim_id
            FROM claims new
            JOIN claims old ON old.id = new.supersedes_claim_id
            WHERE new.supersedes_claim_id IS NOT NULL
            """
        ).fetchall()

    for row in rows:
        assert row["supersedes_claim_id"] == row["old_id"]
    for row in reverse_rows:
        assert row["replaced_by_claim_id"] == row["new_id"]


def test_two_supersessions_one_wins(tmp_path, monkeypatch):
    service = _fresh_service(tmp_path)
    old = _claim(service, "original claim", "old")
    first = _claim(service, "replacement A", "A", confidence=0.8)
    second = _claim(service, "replacement B", "B", confidence=0.9)

    results = _race_supersede(service, old.id, (first.id, second.id), monkeypatch)

    refreshed_old = service.store.get_claim(old.id)
    resolved = [result for result in results if result.get("resolved") is True]
    lost = [result for result in results if result.get("reason") == "lost_race"]

    assert len(resolved) == 1
    assert len(lost) == 1
    assert refreshed_old.status == "superseded"
    assert refreshed_old.replaced_by_claim_id in {first.id, second.id}


def test_no_orphan_supersedes_after_race(tmp_path, monkeypatch):
    service = _fresh_service(tmp_path)
    old = _claim(service, "original claim", "old")
    first = _claim(service, "replacement A", "A", confidence=0.8)
    second = _claim(service, "replacement B", "B", confidence=0.9)

    _race_supersede(service, old.id, (first.id, second.id), monkeypatch)

    _assert_no_orphan_supersedes(service)


def test_retry_after_loss(tmp_path, monkeypatch):
    service = _fresh_service(tmp_path)
    old = _claim(service, "original claim", "old")
    first = _claim(service, "replacement A", "A", confidence=0.8)
    second = _claim(service, "replacement B", "B", confidence=0.9)

    _race_supersede(service, old.id, (first.id, second.id), monkeypatch)
    winner_id = service.store.get_claim(old.id).replaced_by_claim_id
    chained = _claim(service, "replacement C", "C", confidence=1.0)

    monkeypatch.setattr(
        "memorymaster.auto_resolver._llm_evaluate",
        lambda _prompt: {"winner": "B", "reason": "retry after race"},
    )
    winner = service.store.get_claim(winner_id, include_citations=True)
    chained = service.store.get_claim(chained.id, include_citations=True)

    result = resolve_conflict_pair(service.store, winner, chained)

    refreshed_winner = service.store.get_claim(winner_id)
    refreshed_chained = service.store.get_claim(chained.id)
    assert result["resolved"] is True
    assert refreshed_winner.status == "superseded"
    assert refreshed_winner.replaced_by_claim_id == chained.id
    assert refreshed_chained.supersedes_claim_id == winner_id
    _assert_no_orphan_supersedes(service)
