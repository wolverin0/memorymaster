from __future__ import annotations

from memorymaster.core.models import Claim
from memorymaster.core.service import MemoryService
from memorymaster.recall.planner import RetrievalRequest, build_retrieval_plan


QUERY = "why does wezterm cli time out from Node but not from bash"


def _claim(claim_id: int, text: str, human_id: str) -> Claim:
    return Claim(
        id=claim_id,
        text=text,
        idempotency_key=None,
        normalized_text=None,
        claim_type="fact",
        subject=None,
        predicate=None,
        object_value=None,
        scope="project:wezbridge",
        volatility="low",
        status="confirmed",
        confidence=0.765,
        pinned=False,
        supersedes_claim_id=None,
        replaced_by_claim_id=None,
        created_at="2026-08-13T00:00:00+00:00",
        updated_at="2026-08-13T00:00:00+00:00",
        last_validated_at=None,
        archived_at=None,
        human_id=human_id,
        tier="core",
    )


class _Store:
    def __init__(self, claims: list[Claim]) -> None:
        self.claims = claims
        self.limits: list[int] = []

    def list_claims(self, *, limit: int, **_kwargs) -> list[Claim]:
        self.limits.append(limit)
        return self.claims[:limit]


def test_fast_natural_language_path_returns_mm_8aef_in_top_five() -> None:
    distractors = [
        _claim(index, f"WezTerm operational note number {index}", f"mm-decoy-{index}")
        for index in range(1, 8)
    ]
    target = _claim(
        128576,
        "Duplicate wezterm-gui processes cause wezterm CLI ETIMEDOUT from Node while bash stays fast.",
        "mm-8aef",
    )
    store = _Store([*distractors, target])
    service = MemoryService.__new__(MemoryService)
    service.store = store
    service.tenant_id = None
    plan = build_retrieval_plan(RetrievalRequest(query_text=QUERY, limit=5))

    rows = service._query_legacy_mode(
        plan.search_text,
        5,
        ["confirmed"],
        None,
        True,
        None,
        record_accesses=False,
    )

    assert [row["claim"].human_id for row in rows][:1] == ["mm-8aef"]
    assert min(store.limits) >= 60


def test_short_keyword_path_keeps_legacy_candidate_bound() -> None:
    store = _Store([_claim(1, "wezterm note", "mm-short")])
    service = MemoryService.__new__(MemoryService)
    service.store = store
    service.tenant_id = None

    rows = service._query_legacy_mode(
        "wezterm", 5, ["confirmed"], None, True, None, record_accesses=False
    )

    assert [row["claim"].human_id for row in rows] == ["mm-short"]
    assert store.limits == [5]
