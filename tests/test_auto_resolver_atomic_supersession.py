from __future__ import annotations

from pathlib import Path

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.govern.auto_resolver import resolve_conflict_pair


def _claim(service: MemoryService, text: str, value: str):
    return service.ingest(
        text=text,
        citations=[CitationInput(source="test")],
        subject="atomic-resolver",
        predicate="winner",
        object_value=value,
    )


def test_auto_resolver_cannot_leave_one_sided_supersession(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = MemoryService(str(tmp_path / "resolver.db"))
    service.init_db()
    winner = _claim(service, "winner", "new")
    loser = _claim(service, "loser", "old")
    monkeypatch.setattr(
        "memorymaster.govern.auto_resolver._llm_evaluate",
        lambda _prompt: {"winner": "A", "reason": "adversarial atomicity"},
    )

    def fail_legacy_second_step(*_args, **_kwargs) -> None:
        raise RuntimeError("legacy reciprocal-link step failed")

    monkeypatch.setattr(service.store, "set_supersedes", fail_legacy_second_step)

    result = resolve_conflict_pair(service.store, winner, loser)

    refreshed_winner = service.store.get_claim(winner.id, include_citations=False)
    refreshed_loser = service.store.get_claim(loser.id, include_citations=False)
    assert result["resolved"] is True
    assert refreshed_loser.status == "superseded"
    assert refreshed_loser.replaced_by_claim_id == winner.id
    assert refreshed_winner.supersedes_claim_id == loser.id
